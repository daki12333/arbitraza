from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import asyncio

from arb.config import ALLOWED_USERS
from arb import paper, secrets
from arb.tg import keyboards as kb
from arb.tg.formatting import (MIDDLES_PAGE, PAGE_SIZE, TZ, arb_text, list_text, middles_text, money, page_count,
                                paper_report, paper_text, status_text)
from arb.arbitrage import Arb
from arb.tg.service import ArbService, _legs_sig, arb_key
from arb.tg.storage import Storage, UserSettings

log = logging.getLogger(__name__)

MAX_ARBS_SHOWN = 15
MAX_NOTIFY_PER_SCAN = 5
PAPER_PER_ROUND = 3  # paper tests per scan per user (each takes a few seconds)
PAPER_REPORT_HOUR = 23  # the day's paper report is sent once after this hour (Belgrade time)
BUDGET_LIMITS = {"din": (1_000, 10_000_000), "$": (5, 1_000_000)}

# "50000", "50.000", "50 000", "50k", "1,5k", "ulog 50k", "50000 din"
AMOUNT_RE = re.compile(
    r"^\s*(?:(?:ulog|budzet|budžet|imam)\s*:?\s*)?"
    r"(\d{1,3}(?:[ .]\d{3})+|\d+(?:[.,]\d+)?)\s*(k)?\s*(?:din|rsd|\$|usd|usdt)?\s*$",
    re.IGNORECASE,
)

router = Router()
denied = Router()

# Users who pressed "✏️ Upiši svoj iznos": their next number goes either to the
# default stake ("default") or to one arb message (chat_id, message_id, arb key).
awaiting: dict[int, str | tuple[int, int, str]] = {}

LIVE_HOURS = 12  # a live list stops updating itself after this long


@dataclass
class LiveList:
    """The one list message per user that gets re-edited after every scan."""

    chat_id: int
    message_id: int
    live: bool = True
    page: int = 0
    started: float = field(default_factory=time.time)


live_lists: dict[int, LiveList] = {}


def worth_it(arb: Arb, s: UserSettings) -> bool:
    """Only arbs that work for this user's budget are shown / pushed: every leg's stake
    fits what the bookie takes at that odd, and the rounded stakes still make a profit."""
    return arb.plan(s.budget, s.currency) is not None


def list_view(arbs: list[Arb], s: UserSettings) -> list[Arb]:
    """The user's list filter (starts within N hours) and order (profit or kickoff)."""
    if s.list_hours:
        limit = datetime.now(timezone.utc) + timedelta(hours=s.list_hours)
        arbs = [a for a in arbs if a.event.start <= limit]
    if s.list_sort == "time":
        arbs = sorted(arbs, key=lambda a: (a.event.start, -a.profit_pct))
    else:  # the profit for this user's budget (Polymarket's odd depends on the stake)
        arbs = sorted(arbs, key=lambda a: -a.pct_for(s.budget, s.currency))
    return arbs


def render_list(service: ArbService, s: UserSettings, ll: LiveList) -> tuple[str, InlineKeyboardMarkup]:
    all_arbs = [a for a in service.arbs_for(s) if worth_it(a, s)]
    arbs = list_view(all_arbs, s)
    pages = page_count(len(arbs))
    ll.page = min(ll.page, pages - 1)  # the list may have shrunk since last time
    first = ll.page * PAGE_SIZE
    text = list_text(arbs, s.budget, service.age, ll.live, ll.page, s.list_hours, s.list_sort,
                     hidden=len(all_arbs) - len(arbs), currency=s.currency)
    keys = [arb_key(a) for a in arbs[first:first + PAGE_SIZE]]
    return text, kb.list_kb(keys, first, ll.live, ll.page, pages, s.list_hours, s.list_sort)


async def refresh_list(bot: Bot, uid: int, service: ArbService, s: UserSettings) -> None:
    """Redraw the user's open list right away (e.g. an arb just turned out to be gone)."""
    ll = live_lists.get(uid)
    if ll:
        await edit_list(bot, uid, ll, service, s)


async def edit_list(bot: Bot, uid: int, ll: LiveList, service: ArbService, s: UserSettings) -> None:
    text, markup = render_list(service, s, ll)
    try:
        await bot.edit_message_text(text, chat_id=ll.chat_id, message_id=ll.message_id, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        live_lists.pop(uid, None)  # message deleted or too old to edit
    except TelegramNetworkError as e:  # slow / dropped connection: try again after the next scan
        log.warning("list update for %s failed: %s", uid, e)


def odds_age(arb: Arb, service: ArbService) -> float:
    """Seconds since this arb's odds were read (re-check time if it was re-checked)."""
    return time.time() - arb.checked_at if arb.checked_at else service.age


def allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS


router.message.filter(lambda m: m.from_user and allowed(m.from_user.id))
router.callback_query.filter(lambda c: allowed(c.from_user.id))


@denied.message()
async def deny_message(m: Message) -> None:
    await m.answer(f"⛔ Nemaš pristup ovom botu.\nTvoj Telegram ID: <code>{m.from_user.id}</code>")


@denied.callback_query()
async def deny_callback(c: CallbackQuery) -> None:
    await c.answer("⛔ Nemaš pristup.", show_alert=True)


# ---------------------------------------------------------------- menu

@router.message(CommandStart())
async def start(m: Message, store: Storage) -> None:
    s = store.get(m.from_user.id)
    text = (
        "👋 <b>Arb bot – srpske kladionice</b>\n\n"
        "Pratim srpske kladionice (a u 🏦 Kladionice možeš da prebaciš i na 🪙 kripto) "
        "i izbacujem <b>sve arbitraže</b>.\n\n"
        "Za svaku ti kažem koliko da uložiš na koju kvotu i koliki je profit, "
        "plus 🔗 <b>linkove direktno na utakmicu</b> u svakoj kladionici.\n\n"
        f"💰 Ulog: <b>{money(s.budget, s.currency)}</b> {s.currency} – promeni ga tako što samo upišeš broj (npr. <code>50000</code>)."
    )
    if not ALLOWED_USERS:
        text += (
            f"\n\n🔓 Bot je otvoren za sve. Tvoj ID je <code>{m.from_user.id}</code> – "
            "upiši ga u <code>.env</code> kao <code>ALLOWED_USERS</code> da ga zaključaš."
        )
    await m.answer(text, reply_markup=kb.main_menu(s))


async def send_arbs(m: Message, s: UserSettings, service: ArbService) -> None:
    wait = None
    if not service.covers(s):
        wait = await m.answer("⏳ Skeniram kladionice…")
    await service.fresh(s)
    if wait:
        await wait.delete()

    arbs = [a for a in service.arbs_for(s) if worth_it(a, s)]
    if not arbs:
        n = sum(1 for g in service.result.groups if len(g) > 1)
        await m.answer(
            f"😴 Trenutno nema nijedne arbitraže (upoređeno {n} utakmica).\n"
            "Javiću ti čim nešto iskoči ako su 🔔 obaveštenja uključena."
        )
        return
    extra = f", prikazujem najboljih {MAX_ARBS_SHOWN}" if len(arbs) > MAX_ARBS_SHOWN else ""
    await m.answer(
        f"💰 Našao sam <b>{len(arbs)}</b> arbitraža{extra}. Ulog <b>{money(s.budget, s.currency)}</b> {s.currency}, najveći profit prvi:"
    )
    for arb in arbs[:MAX_ARBS_SHOWN]:
        await m.answer(arb_text(arb, s.budget, service.age, s.currency), reply_markup=kb.arb_kb(arb, arb_key(arb), s.budget))


@router.message(Command("arbitraze", "lista"))
@router.message(F.text == kb.BTN_LIST)
async def show_list(m: Message, bot: Bot, service: ArbService, store: Storage) -> None:
    s = store.get(m.from_user.id)
    wait = None
    if not service.covers(s):
        wait = await m.answer("⏳ Skeniram kladionice…")
    await service.fresh(s)
    if wait:
        await wait.delete()

    # only the newest list is kept live; freeze the previous one
    old = live_lists.pop(m.from_user.id, None)
    if old and old.live:
        old.live = False
        await edit_list(bot, m.from_user.id, old, service, s)

    ll = LiveList(0, 0)
    text, markup = render_list(service, s, ll)
    msg = await m.answer(text, reply_markup=markup)
    ll.chat_id, ll.message_id = msg.chat.id, msg.message_id
    live_lists[m.from_user.id] = ll


@router.message(F.text == kb.BTN_ARBS)
async def show_arbs(m: Message, service: ArbService, store: Storage) -> None:
    await send_arbs(m, store.get(m.from_user.id), service)


@router.message(F.text.startswith(kb.BTN_BUDGET))
async def ask_budget(m: Message, store: Storage) -> None:
    s = store.get(m.from_user.id)
    await m.answer(
        f"💰 <b>Koliko ulažeš po arbitraži?</b>  (sada: {money(s.budget, s.currency)} {s.currency})\n"
        f"Izaberi ili samo upiši broj, npr. <code>{example(s)}</code>.",
        reply_markup=kb.budget_kb(s),
    )


@router.message(F.text == kb.BTN_BOOKIES)
async def show_bookies(m: Message, store: Storage) -> None:
    s = store.get(m.from_user.id)
    await m.answer(bookies_text(s), reply_markup=kb.bookies_kb(s))


def bookies_text(s: UserSettings) -> str:
    if s.mode == "crypto":
        mode = "🪙 <b>kripto kladionice</b> (ulog u $)"
        if not s.mode_bookies:
            return f"🏦 <b>Kladionice</b> – režim: {mode}\nJoš nema nijedne kripto kladionice."
    else:
        mode = "🇷🇸 <b>srpske kladionice</b> (ulog u din)"
    return f"🏦 <b>Kladionice</b> – režim: {mode}\nUključi samo one na kojima imaš nalog:"


def number(text: str) -> float:
    """Callback amount: 50000 -> 50000 (int), 10.5 -> 10.5."""
    x = float(text)
    return int(x) if x.is_integer() else x


def parse_amount(num: str, thousands: bool, currency: str) -> float | None:
    """Typed amount. Dinars: "50.000" / "50,000" are thousands separators.
    Dollars: "10.5" / "10,5" is a decimal amount, rounded to half a dollar
    ("1.000" with exactly 3 digits after the dot still means a thousand)."""
    try:
        if thousands:
            x = float(num.replace(",", ".")) * 1000
        elif currency == "$" and re.fullmatch(r"\d+[.,]\d{1,2}", num):
            x = float(num.replace(",", "."))
        else:
            x = float(num.replace(".", "").replace(",", ""))
    except ValueError:
        return None
    if currency == "$":
        return number(str(round(x * 2) / 2))
    return int(round(x))


def example(s: UserSettings) -> str:
    return "150</code> ili <code>10.5" if s.currency == "$" else "37500</code> ili <code>37.5k"


@router.message(F.text == kb.BTN_MIDDLES)
async def show_middles(m: Message, service: ArbService, store: Storage) -> None:
    s = store.get(m.from_user.id)
    wait = await m.answer("⏳ Tražim srednjice…") if not service.covers(s) else None
    await service.fresh(s)
    if wait:
        await wait.delete()
    text, markup = render_middles(service, s, 0)
    await m.answer(text, reply_markup=markup)


def render_middles(service: ArbService, s: UserSettings, page: int):
    middles = service.middles_for(s)
    pages = max(1, -(-len(middles) // MIDDLES_PAGE))
    page = min(page, pages - 1)
    return middles_text(middles, s.budget, s.currency, page, service.age), kb.middles_kb(page, pages)


@router.callback_query(F.data.startswith("md:"))
async def cb_middles(c: CallbackQuery, service: ArbService, store: Storage) -> None:
    s = store.get(c.from_user.id)
    page = int(c.data.split(":")[2])
    text, markup = render_middles(service, s, page)
    await safe_edit(c, text, markup)
    await c.answer()


@router.message(F.text == kb.BTN_STATUS)
async def show_status(m: Message, service: ArbService) -> None:
    await m.answer(status_text(service.result, service.age, service.interval))


def notify_text(s: UserSettings) -> str:
    when = f"utakmica počinje u narednih <b>{s.notify_hours} h</b>" if s.notify_hours else "utakmica počinje <b>bilo kad</b>"
    state = (f"uključena\n\nObavesti me kad je arbitraža <b>{s.notify_min:g}%</b> ili više i {when}"
             if s.notify else "<b>isključena</b>")
    text = (
        f"🔔 <b>Obaveštenja</b>: {state}\n\n"
        "Gore biraš najmanji profit (%), dole koliko brzo utakmica počinje.\n"
        "📋 Lista arbitraža prikazuje sve koje prolaze za tvoj ulog, bez obzira na ovo."
    )
    if s.mode == "crypto":
        text += ("\n\n🧪 <b>Test na papiru</b>: " + ("uključen" if s.paper else "isključen") +
                 " – bot „igra“ arbitraže po ovim istim pravilima, ali ništa ne uplaćuje, i javi ti da li bi "
                 "prošle (kvote proverene uživo, druga noga posle 2 s). Uveče stiže izveštaj.")
    return text


def paper_report_for(uid: int, s: UserSettings) -> str:
    """Today's paper tests (since midnight, Belgrade time)."""
    midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return paper_report(paper.load(uid, midnight.timestamp()), "danas (od 00:00)", s.currency)


def notify_wanted(arb: Arb, s: UserSettings) -> bool:
    """Does this arb pass the user's notification rules (min profit, kickoff within N hours)?"""
    if s.notify_hours and arb.event.start > datetime.now(timezone.utc) + timedelta(hours=s.notify_hours):
        return False
    return worth_it(arb, s) and arb.pct_for(s.budget, s.currency) >= s.notify_min


@router.message(F.text.startswith(kb.BTN_NOTIFY) | F.text.startswith("🔕 Obaveštenja"))
async def show_notify(m: Message, store: Storage) -> None:
    s = store.get(m.from_user.id)
    await m.answer(notify_text(s), reply_markup=kb.notify_kb(s))


PERCENT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*%?\s*$")


@router.message(F.text, lambda m: awaiting.get(m.from_user.id) == "sxkey")
async def typed_sx_key(m: Message, store: Storage) -> None:
    awaiting.pop(m.from_user.id, None)
    key = (m.text or "").strip()
    try:
        await m.delete()  # don't leave the key in the chat
    except TelegramBadRequest:
        pass
    ok = await check_sx_key(key)
    if ok is False:
        await m.answer("❌ SX Bet ne prihvata taj ključ. Klikni 🔑 opet i pošalji tačan ključ.")
        return
    secrets.put("sxbet_api_key", key)
    s = store.get(m.from_user.id)
    note = "" if ok else " (SX Bet se trenutno ne javlja, ključ je ipak sačuvan)"
    await m.answer(f"✅ SX Bet ključ sačuvan{note}. Kvote sa SX Bet-a ulaze od sledećeg scana.",
                   reply_markup=kb.bookies_kb(s))


async def check_sx_key(key: str) -> bool | None:
    """True = SX accepts it, False = rejected, None = couldn't check (network)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.sx.bet/markets/active", params={"onlyMainLine": "true", "pageSize": 1})
            h = r.json()["data"]["markets"][0]["marketHash"]
            r = await c.get("https://api.sx.bet/orders-v3/odds/best", params={"marketHashes": h},
                            headers={"x-sx-api-key": key})
        return r.status_code != 401
    except Exception:
        return None


@router.message(F.text, lambda m: awaiting.get(m.from_user.id) == "notify")
async def typed_notify_min(m: Message, store: Storage) -> None:
    match = PERCENT_RE.match(m.text)
    pct = float(match.group(1).replace(",", ".")) if match else -1
    if not 0 <= pct <= 50:
        await m.answer("Upiši procenat između 0 i 50, npr. <code>1.5</code>")
        return
    awaiting.pop(m.from_user.id, None)
    s = store.get(m.from_user.id)
    s.notify_min, s.notify = pct, True
    store.save()
    await m.answer(notify_text(s), reply_markup=kb.notify_kb(s))


@router.message(F.text, lambda m: awaiting.get(m.from_user.id) == "notify_h")
async def typed_notify_hours(m: Message, store: Storage) -> None:
    match = re.match(r"^\s*(\d+)\s*(h|sat[ai]?)?\s*$", m.text, re.I)
    hours = int(match.group(1)) if match else -1
    if not 0 <= hours <= 24 * 14:
        await m.answer("Upiši broj sati, npr. <code>24</code> (0 = bilo kad)")
        return
    awaiting.pop(m.from_user.id, None)
    s = store.get(m.from_user.id)
    s.notify_hours, s.notify = hours, True
    store.save()
    await m.answer(notify_text(s), reply_markup=kb.notify_kb(s))


@router.message(F.text.regexp(AMOUNT_RE))
async def typed_amount(m: Message, bot: Bot, service: ArbService, store: Storage) -> None:
    num, k = AMOUNT_RE.match(m.text).groups()
    num = num.replace(" ", "")
    s = store.get(m.from_user.id)
    amount = parse_amount(num, bool(k), s.currency)
    if amount is None:
        await m.answer(f"Ne razumem iznos. Upiši npr. <code>{example(s)}</code>")
        return
    low, high = BUDGET_LIMITS[s.currency]
    if not low <= amount <= high:
        await m.answer(f"Ulog mora biti između {money(low, s.currency)} i {money(high, s.currency)} {s.currency}. Upiši ponovo:")
        return
    target = awaiting.pop(m.from_user.id, "default")

    if isinstance(target, tuple):  # custom stake for one arb message
        chat_id, message_id, key = target
        await service.fresh(s)
        arb = service.lookup(key, s)
        if not arb:
            await m.answer("❌ Ta arbitraža više ne postoji (kvote su se promenile).")
            return
        try:
            await bot.edit_message_text(
                arb_text(arb, amount, service.age, s.currency), chat_id=chat_id, message_id=message_id,
                reply_markup=kb.arb_kb(arb, key, amount),
            )
            await m.answer(f"✅ Preračunato za <b>{money(amount, s.currency)}</b> {s.currency} ☝️")
        except TelegramBadRequest:  # original message deleted / too old - send a fresh one
            await m.answer(arb_text(arb, amount, service.age, s.currency), reply_markup=kb.arb_kb(arb, key, amount))
        return

    s.budget = amount
    store.save()
    await m.answer(f"✅ Ulog: <b>{money(amount, s.currency)}</b> {s.currency}", reply_markup=kb.main_menu(s))
    await send_arbs(m, s, service)


@router.message()
async def fallback(m: Message, store: Storage) -> None:
    s = store.get(m.from_user.id)
    if m.from_user.id in awaiting:
        await m.answer(f"Upiši samo iznos ({s.currency}), npr. <code>{example(s)}</code>.")
        return
    await m.answer(
        f"Upiši koliko ulažeš (npr. <code>{example(s)}</code>) ili koristi dugmiće ispod 👇",
        reply_markup=kb.main_menu(s),
    )


# ------------------------------------------------------------ callbacks

async def safe_edit(c: CallbackQuery, text: str | None = None, markup=None) -> None:
    try:
        if text is not None:
            await c.message.edit_text(text, reply_markup=markup)
        else:
            await c.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@router.errors(lambda e: isinstance(e.exception, TelegramBadRequest) and "query is too old" in str(e.exception))
async def old_click(event) -> bool:
    """A button pressed while the bot was restarting: Telegram no longer takes the answer. Nothing to do."""
    return True


@router.callback_query(F.data == "del")
async def cb_delete(c: CallbackQuery) -> None:
    try:
        await c.message.delete()
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("ad:"))
async def cb_arb_detail(c: CallbackQuery, bot: Bot, service: ArbService, store: Storage) -> None:
    _, gkey, market = c.data.split(":")
    key = f"{gkey}:{market}"
    s = store.get(c.from_user.id)
    await c.answer()
    # answer at once, then fill in the re-checked odds (slow sites: max ~10 s)
    msg = await c.message.answer("⏳ Proveravam trenutne kvote na kladionicama…")
    arb = await service.lookup_fresh(key, s)
    if not arb:
        await msg.edit_text("❌ Ta arbitraža više ne postoji – kvote su se u međuvremenu promenile. Sklonjena je iz liste.")
        await refresh_list(bot, c.from_user.id, service, s)
        return
    await msg.edit_text(arb_text(arb, s.budget, odds_age(arb, service), s.currency),
                        reply_markup=kb.arb_kb(arb, key, s.budget))
    await refresh_list(bot, c.from_user.id, service, s)  # the list shows the re-checked %


@router.callback_query(F.data.startswith("ls:"))
async def cb_list(c: CallbackQuery, bot: Bot, service: ArbService, store: Storage) -> None:
    parts = c.data.split(":")
    action = parts[1]
    uid = c.from_user.id
    s = store.get(uid)
    ll = live_lists.get(uid)
    if not ll or ll.message_id != c.message.message_id:
        # an older list: adopt it as the tracked one
        if ll and ll.live:
            ll.live = False
            await edit_list(bot, uid, ll, service, s)
        ll = live_lists[uid] = LiveList(c.message.chat.id, c.message.message_id, live=False)

    if action == "refresh":
        await service.fresh(s)
    elif action == "page":
        ll.page = int(parts[2])
    elif action == "hours":
        s.list_hours, ll.page = int(parts[2]), 0
        store.save()
    elif action == "sort":
        s.list_sort, ll.page = ("time" if s.list_sort == "pct" else "pct"), 0
        store.save()
    elif action == "stop":
        ll.live = False
    elif action == "live":
        ll.live, ll.started = True, time.time()
    await edit_list(bot, uid, ll, service, s)
    await c.answer({"refresh": "Osveženo", "stop": "Praćenje zaustavljeno", "live": "Pratim uživo",
                    "page": f"Strana {ll.page + 1}",
                    "hours": f"Prikazujem: {'sve' if not s.list_hours else f'narednih {s.list_hours}h'}",
                    "sort": "Sortirano po vremenu" if s.list_sort == "time" else "Sortirano po profitu"}.get(action, ""))


@router.callback_query(F.data.startswith("cu:"))
async def cb_custom_arb(c: CallbackQuery, store: Storage) -> None:
    _, gkey, market, _ = c.data.split(":")
    awaiting[c.from_user.id] = (c.message.chat.id, c.message.message_id, f"{gkey}:{market}")
    s = store.get(c.from_user.id)
    await c.message.answer(f"✏️ Upiši koliko ulažeš u ovu arbitražu (npr. <code>{example(s)}</code>):")
    await c.answer()


@router.callback_query(F.data.startswith("rf:") | F.data.startswith("st:"))
async def cb_arb(c: CallbackQuery, bot: Bot, service: ArbService, store: Storage) -> None:
    action, gkey, market, budget = c.data.split(":")
    key, budget = f"{gkey}:{market}", number(budget)
    s = store.get(c.from_user.id)

    if action == "st":
        await safe_edit(c, markup=kb.arb_budget_kb(key, budget, s.currency))
        await c.answer("Izaberi ulog")
        return

    await c.answer("⏳ Proveravam kvote samo za ovaj meč…")
    arb = await service.lookup_fresh(key, s)
    if not arb:
        await safe_edit(c, (c.message.html_text or "") + "\n\n❌ <b>Arbitraža više ne postoji</b> (kvote su se promenile). Sklonjena je iz liste.")
        await refresh_list(bot, c.from_user.id, service, s)
        return
    await safe_edit(c, arb_text(arb, budget, odds_age(arb, service), s.currency), kb.arb_kb(arb, key, budget))
    await refresh_list(bot, c.from_user.id, service, s)  # the list shows the re-checked %


@router.callback_query(F.data.startswith("nt:"))
async def cb_notify(c: CallbackQuery, store: Storage) -> None:
    s = store.get(c.from_user.id)
    parts = c.data.split(":")
    if parts[1] == "custom":
        awaiting[c.from_user.id] = "notify"
        await c.message.answer("✏️ Upiši minimalni profit u % za obaveštenja (npr. <code>1.5</code>):")
        await c.answer()
        return
    if parts[1] == "prep":
        await c.answer()
        await c.message.answer(paper_report_for(c.from_user.id, s))
        return
    if parts[1] == "paper":
        s.paper = not s.paper
        store.save()
        await safe_edit(c, notify_text(s), kb.notify_kb(s))
        await c.answer("🧪 Test uključen – prve rezultate dobijaš posle sledećeg skeniranja" if s.paper
                       else "Test isključen")
        return
    if parts[1] == "hcustom":
        awaiting[c.from_user.id] = "notify_h"
        await c.message.answer("✏️ Upiši u koliko narednih sati utakmica treba da počinje (npr. <code>24</code>, 0 = bilo kad):")
        await c.answer()
        return
    if parts[1] == "toggle":
        s.notify = not s.notify
    elif parts[1] == "min":
        s.notify_min, s.notify = float(parts[2]), True
    elif parts[1] == "h":
        s.notify_hours, s.notify = int(parts[2]), True
    store.save()
    await safe_edit(c, notify_text(s), kb.notify_kb(s))
    await c.answer("Sačuvano")


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(c: CallbackQuery, service: ArbService, store: Storage) -> None:
    s = store.get(c.from_user.id)
    _, what, value = c.data.split(":", 2)

    if what == "custom":
        awaiting[c.from_user.id] = "default"
        await c.message.answer(f"✏️ Upiši koliko ulažeš po arbitraži (npr. <code>{example(s)}</code>):")
    elif what == "budget":
        awaiting.pop(c.from_user.id, None)
        s.budget = number(value)
        store.save()
        await safe_edit(c, f"✅ Ulog: <b>{money(s.budget, s.currency)}</b> {s.currency}. Klikni 🔍 Arbitraže.", kb.budget_kb(s))
        await c.message.answer("Meni osvežen 👇", reply_markup=kb.main_menu(s))
    elif what == "sxkey":
        awaiting[c.from_user.id] = "sxkey"
        await c.message.answer(
            "🔑 Pošalji mi SX Bet API ključ kao običnu poruku.\n"
            "Odmah ću obrisati tvoju poruku i sačuvati ključ – unosi se samo jednom.")
    elif what == "mode":
        awaiting.pop(c.from_user.id, None)
        s.switch_mode()
        store.save()
        await c.answer("Prebačeno")
        await safe_edit(c, bookies_text(s), kb.bookies_kb(s))
        where = "🪙 kripto kladionice" if s.mode == "crypto" else "🇷🇸 srpske kladionice"
        msg = await c.message.answer(f"✅ Prebačeno na {where}. Ulog: <b>{money(s.budget, s.currency)}</b> {s.currency}",
                                     reply_markup=kb.main_menu(s))
        if not service.covers(s):  # nobody used this region: scan it now (~30-60 s the first time)
            wait = await c.message.answer("⏳ Učitavam kvote sa tih kladionica, može potrajati do minut…")
            await service.fresh(s)
            await wait.edit_text(f"✅ Kvote učitane – {len(service.arbs_for(s))} arbitraža. Klikni 📋 Lista arbitraža.")
        return
    elif what == "bk" and value in s.mode_bookies:
        if value in s.disabled:
            s.disabled.remove(value)
        else:
            if len(s.bookies) <= 2:
                await c.answer("Moraju ostati bar 2 kladionice.", show_alert=True)
                return
            s.disabled.append(value)
        store.save()
        await safe_edit(c, bookies_text(s), kb.bookies_kb(s))
    await c.answer()


# -------------------------------------------------------- notifications

class Notifier:
    """After each scan, pushes arbs the user hasn't been told about yet."""

    def __init__(self, bot: Bot, service: ArbService, store: Storage) -> None:
        self.bot, self.service, self.store = bot, service, store
        self.sent: dict[int, set[str]] = {}
        self.paper_task: asyncio.Task | None = None
        self.paper_seen: dict[int, dict[str, tuple[float, tuple]]] = {}  # uid -> arb key -> (tested at, legs)
        self.paper_reported: dict[int, str] = {}  # uid -> date of the last evening report

    async def __call__(self) -> None:
        await self.update_lists()
        await self.notify_new()
        # paper tests run in the background: they wait for bookies and must not hold up the scans
        if (self.paper_task is None or self.paper_task.done()) and any(
                s.paper and s.mode == "crypto" for s in self.store.users.values()):
            self.paper_task = asyncio.create_task(self.paper_round())

    async def paper_round(self) -> None:
        for uid, s in list(self.store.users.items()):
            if not (s.paper and s.mode == "crypto" and allowed(uid) and self.service.covers(s)):
                continue
            try:
                await self._paper_user(uid, s)
            except Exception:
                log.exception("paper round %s failed", uid)

    async def _paper_user(self, uid: int, s: UserSettings) -> None:
        seen = self.paper_seen.setdefault(uid, {})
        now = time.time()
        todo = []
        for a in self.service.arbs_for(s):
            if a.suspicious or not notify_wanted(a, s):
                continue
            prev = seen.get(arb_key(a))
            if prev and prev[1] == _legs_sig(a) and now - prev[0] < paper.RETEST_AFTER:
                continue  # tested already with these same odds
            todo.append(a)
        for a in todo[:PAPER_PER_ROUND]:
            seen[arb_key(a)] = (time.time(), _legs_sig(a))
            r = await paper.run_test(self.service, a, s, s.currency)
            await asyncio.to_thread(paper.save, uid, r)
            log.info("paper %s: %s %s %.2f", uid, r.status, r.key, r.profit)
            if r.status in (paper.GONE, paper.NO_FIT):
                continue  # nothing would have been bet - only counted in the report
            try:
                await self.bot.send_message(uid, paper_text(r, s.currency))
            except TelegramForbiddenError:
                s.paper = False
                self.store.save()
                return
        local = datetime.now(TZ)
        if local.hour >= PAPER_REPORT_HOUR and self.paper_reported.get(uid) != local.date().isoformat():
            self.paper_reported[uid] = local.date().isoformat()
            await self.bot.send_message(uid, paper_report_for(uid, s))

    async def update_lists(self) -> None:
        for uid, ll in list(live_lists.items()):
            if not ll.live or not allowed(uid):
                continue
            if time.time() - ll.started > LIVE_HOURS * 3600:
                ll.live = False  # final edit shows "praćenje zaustavljeno"
            await edit_list(self.bot, uid, ll, self.service, self.store.get(uid))

    async def notify_new(self) -> None:
        for uid, s in list(self.store.users.items()):
            if not s.notify or not allowed(uid):
                continue
            if not self.service.covers(s):
                continue
            arbs = [a for a in self.service.arbs_for(s) if notify_wanted(a, s)]
            keys = {arb_key(a) for a in arbs}
            already = self.sent.get(uid, set())
            # forget arbs that disappeared, so they notify again if they come back
            self.sent[uid] = already & keys
            new = [a for a in arbs if arb_key(a) not in already][:MAX_NOTIFY_PER_SCAN]
            # re-check with the bookies' odds right now: only arbs that still exist get pushed
            verified = [a for a in await self.service.verify(new, s) if notify_wanted(a, s)]
            for arb in verified:
                key = arb_key(arb)
                try:
                    await self.bot.send_message(
                        uid, "🚨 <b>Nova arbitraža!</b> (kvote upravo proverene)\n\n"
                        + arb_text(arb, s.budget, odds_age(arb, self.service), s.currency),
                        reply_markup=kb.arb_kb(arb, key, s.budget),
                    )
                    self.sent[uid].add(key)
                except TelegramForbiddenError:  # user blocked the bot
                    s.notify = False
                    self.store.save()
                    break
                except Exception:
                    log.exception("notify %s failed", uid)
