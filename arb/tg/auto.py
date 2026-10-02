"""/bot: real automatic betting on SX Bet + Polymarket (arb.live) - setup of both accounts,
balances, rules, tickets, 📈 Procena (how many arbs there are and what they'd make), 🛑 Stop.
Opened with /bot, the 🤖 Bot button (crypto mode) or 🏦 Kladionice. The paper test is on /bottest."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from arb import secrets
from arb.live import book as bk, stats
from arb.live.engine import (AUTO_MIN_STAKE, PM, SX, UNCONFIRMED, AutoTrader, auto_ok, auto_view, is_pair,
                             report_text, ticket_text)
from arb.live.polymarket import ADDRESS_RE, KEY_RE
from arb.models import market_label
from arb.tg import keyboards as kb
from arb.tg.formatting import TZ, dur, money, signed
from arb.tg.handlers import allowed, awaiting, safe_edit
from arb.tg.storage import Storage, UserSettings

log = logging.getLogger(__name__)

router = Router()
router.message.filter(lambda m: m.from_user and allowed(m.from_user.id))
router.callback_query.filter(lambda c: allowed(c.from_user.id))

STAKES = [2, 5, 10, 25, 50]
DAILY = [25, 50, 100, 250]
OPEN = [25, 50, 100, 250]
MINS = [0.5, 1.0, 1.5, 2.0, 3.0]
HOURS = [1, 3, 6, 12, 24, 0]
_pm_setup: dict[int, dict] = {}  # uid -> {"sig": 1/2, "funder": "0x..."} while connecting Polymarket


# ---------------------------------------------------------------- texts

def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m. %H:%M") if ts else "—"


def _check(ok: bool) -> str:
    return "✅" if ok else "⬜"


async def auto_text(uid: int, s: UserSettings, trader: AutoTrader, service=None) -> str:
    on = "✅ <b>uključeno</b>" if s.auto else "⬜ <b>isključeno</b>"
    lines = [f"🤖 <b>Automatsko klađenje · SX Bet + Polymarket</b>: {on}"]
    if trader.halted:
        lines.append("🛑 <b>Zaustavljeno dugmetom STOP</b> – ništa novo ne ide dok ne uključiš ponovo.")
    lines.append("✋ Pre svake uplate te pita (dugme ✅ Uplati)" if s.auto_confirm
                 else "💸 Uplaćuje samo, bez pitanja")

    sx_key = bool(secrets.get("sxbet_api_key"))
    sx_ok = await trader.ensure_sx()
    pm_ok = await trader.ensure_poly(s)
    bal = await trader.balances(s)
    lines += ["", "<b>🛠 Podešavanje</b> (redom):",
              f"{_check(s.mode == 'crypto')} 1. 🪙 kripto režim (🏦 Kladionice → 🪙 Prebaci na kripto)",
              f"{_check(sx_key)} 2. 🔑 SX Bet API ključ (sx.bet → Settings → API key)",
              f"{_check(sx_ok)} 3. 🔵 SX Bet novčanik (privatni ključ istog naloga)",
              f"{_check(pm_ok)} 4. 🟣 Polymarket nalog (adresa + privatni ključ)",
              f"{_check(bool(bal[SX]) and bool(bal[PM]))} 5. 💵 novac na obe strane (USDC)",
              f"{_check(s.auto)} 6. 🤖 uključi (prvo probaj 🔍 Proba bez uplate)"]

    lines += ["", "<b>🏦 Nalozi</b>"]
    if sx_ok:
        lines.append(f"🔵 SX Bet: ✅ {trader.sx.address[:6]}…{trader.sx.address[-4:]} · balans "
                     + (f"<b>{money(bal[SX], '$')} $</b>" if bal[SX] is not None else f"? ({trader.sx.error})"))
    else:
        lines.append("🔵 SX Bet: ⬜ nije povezan" + (f" ({trader.sx.error})" if trader.sx.error else ""))
    if pm_ok:
        kind = "email/Google" if s.poly_sig == 1 else "MetaMask"
        lines.append(f"🟣 Polymarket: ✅ {s.poly_funder[:6]}…{s.poly_funder[-4:]} ({kind}) · balans "
                     + (f"<b>{money(bal[PM], '$')} $</b>" if bal[PM] is not None else f"? ({trader.poly.error})"))
    else:
        lines.append("🟣 Polymarket: ⬜ nije povezan" + (f" ({trader.poly.error})" if trader.poly.error else ""))

    day = trader.book.day(uid, trader.day_start())
    lines += ["", f"<b>📒 Danas</b>: arbitraža {day['runs']} · uloženo {money(day['staked'], '$')} / "
                  f"{money(s.auto_daily, '$')} $ · u igri {money(day['open'], '$')} / {money(s.auto_open, '$')} $ · "
                  f"završeno {signed(day['settled_profit'], '$')} $"]
    if day["exposed"]:
        lines.append(f"🚨 Otvoreno samo jedno: <b>{day['exposed']}</b> – vidi 📒 Tiketi")
    when = f"u narednih {s.auto_hours} h" if s.auto_hours else "bilo kad"
    lines.append(f"<b>⚙️ Pravila</b>: do <b>{money(s.auto_stake, '$')} $</b> po arbitraži · profit ≥ "
                 f"<b>{s.auto_min:g}%</b> · mečevi {when}")
    if service is not None and s.mode == "crypto" and service.covers(s):
        arbs = [a for a in service.arbs_for(auto_view(s)) if is_pair(a)]
        n = sum(1 for a in arbs if auto_ok(a, s) is None)
        lines.append(f"🔎 Sad: <b>{len(arbs)}</b> arbitraža SX Bet + Polymarket, tvoja pravila prolazi <b>{n}</b>")
    if s.mode != "crypto":
        lines.append("\n⚠️ Radi samo u 🪙 kripto režimu (🏦 Kladionice → 🪙 Prebaci na kripto).")
    lines += ["", "Redosled: prvo SX Bet „sve ili ništa“ po planiranoj kvoti (ako ne prođe, ništa se ne gubi), "
                  "pa Polymarket „sve ili ništa“ po ceni na kojoj je arbitraža najgore na nuli. Pre svake uplate: "
                  "provera kvota, limita, balansa i da li Polymarket dozvoljava tvoju zemlju.",
              "🧪 Test na papiru (ništa se ne uplaćuje) je na /bottest."]
    return "\n".join(lines)


def auto_kb(s: UserSettings, trader: AutoTrader) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Auto: ✅ uključeno (klik = isključi)" if s.auto else "🤖 Auto: ⬜ isključeno (klik = uključi)",
             callback_data="au:toggle")
    b.button(text="✋ Pitaj pre uplate: ✅ da" if s.auto_confirm else "✋ Pitaj pre uplate: ⬜ ne (sam uplaćuje)",
             callback_data="au:confirm")
    b.button(text="🔑 SX API ključ ✅" if secrets.get("sxbet_api_key") else "🔑 SX API ključ", callback_data="set:sxkey:")
    b.button(text="🔵 SX Bet ✅ (promeni)" if trader.sx.connected else "🔵 Poveži SX Bet", callback_data="au:sx")
    b.button(text="🟣 Polymarket ✅ (promeni)" if trader.poly.connected else "🟣 Poveži Polymarket",
             callback_data="au:pm")
    b.button(text="🔄 Osveži balans", callback_data="au:bal")
    b.button(text="📈 Procena (koliko arbitraža ima)", callback_data="au:est")
    b.button(text="⚙️ Pravila", callback_data="au:rules")
    b.button(text="📒 Tiketi", callback_data="au:tickets")
    b.button(text="📊 Izveštaj", callback_data="au:report")
    b.button(text="🔍 Proba bez uplate", callback_data="au:dry")
    b.button(text="🛑 STOP – zaustavi sve", callback_data="au:stop")
    b.button(text="📒 Tiketi i balans – sve kladionice (igraš ti, bot prati)", callback_data="tr:new")
    b.adjust(1, 1, 3, 1, 1, 3, 1, 1, 1)
    return b.as_markup()


def rules_text(s: UserSettings) -> str:
    when = f"u narednih <b>{s.auto_hours} h</b>" if s.auto_hours else "<b>bilo kad</b>"
    return "\n".join([
        "⚙️ <b>Pravila za prave uplate</b>", "",
        f"💵 najviše <b>{money(s.auto_stake, '$')} $</b> po arbitraži (ukupno na obe strane)",
        f"📅 najviše <b>{money(s.auto_daily, '$')} $</b> uloženo dnevno",
        f"⏳ najviše <b>{money(s.auto_open, '$')} $</b> u tiketima čiji meč još nije završen",
        f"📈 profit najmanje <b>{s.auto_min:g}%</b> (posle naknada obe berze)",
        f"⏰ meč počinje {when}",
        "", f"💡 Kreni sa malim ulozima (2–5 $). Najmanje {AUTO_MIN_STAKE:g} $ ukupno (svaka berza prima od 1 $).",
    ])


def rules_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    rows = []
    for vals, cur, code, fmt in ((STAKES, s.auto_stake, "st", "💵 {} $"), (DAILY, s.auto_daily, "dl", "📅 {} $"),
                                 (OPEN, s.auto_open, "op", "⏳ {} $"), (MINS, s.auto_min, "min", "📈 {}%")):
        for v in vals:
            b.button(text=("✅ " if v == cur else "") + fmt.format(f"{v:g}"), callback_data=f"au:{code}:{v}")
        b.button(text="✏️", callback_data=f"au:c{code}")
        rows.append(len(vals) + 1)
    for h in HOURS:
        b.button(text=("✅ " if h == s.auto_hours else "") + (f"⏰ {h}h" if h else "bilo kad"), callback_data=f"au:h:{h}")
    rows.append(len(HOURS))
    b.button(text="⬅️ Nazad", callback_data="au:back")
    rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


async def estimate_text(uid: int, s: UserSettings, trader: AutoTrader) -> str:
    bal = await trader.balances(s)
    capital = (bal[SX] or 0) + (bal[PM] or 0) or None
    e = stats.estimate(trader.book, s.auto_stake, s.auto_min, capital)
    head = "📈 <b>Procena – arbitraže SX Bet + Polymarket</b>"
    if e is None or e["hours"] < 0.05:
        return (f"{head}\n\nJoš nema podataka. Bot beleži svaku arbitražu između ove dve berze posle svakog "
                "skeniranja (dok je neko u 🪙 kripto režimu i SX API ključ je unet). Pogledaj ponovo za sat-dva, "
                "a pravu sliku daje 24 h.")
    lines = [head, f"Na osnovu poslednjih <b>{dur(e['hours'] * 3600)}</b> skeniranja"
                   + (" (pravu sliku daje 24 h)" if e["hours"] < 23 else "") + ":", "",
             f"🔎 različitih arbitraža: <b>{e['all']}</b>, od toga ≥ {s.auto_min:g}%: <b>{e['good']}</b> "
             f"(≈ <b>{e['per_day']:.0f}</b> dnevno)"]
    if e["good"]:
        lines += [f"📈 tipičan profit: <b>{e['median_pct']:.2f}%</b> · tipično prima do <b>{money(e['median_cap'], '$')} $</b>"
                  f" · traje oko <b>{dur(e['median_lasted'])}</b>",
                  "🏟 " + ", ".join(f"{sp} {n}" for sp, n in e["sports"]), "",
                  f"Sa ulogom do <b>{money(s.auto_stake, '$')} $</b> po arbitraži, kad bi bot uhvatio <b>svaku</b>:",
                  f"💵 uloženo ≈ {money(e['staked_per_day'], '$')} $ dnevno → zarada ≈ <b>{money(e['profit_per_day'], '$')} $"
                  "</b> dnevno"]
        if e["pct_of_capital"] is not None:
            lines.append(f"📊 to je ≈ <b>{e['pct_of_capital']:.2f}%</b> dnevno na tvoj kapital "
                         f"({money(capital, '$')} $ na obe berze)")
        lines += ["", "Najbolje:"]
        for r in e["top"]:
            lines.append(f"• {r['name'][:34]} · {market_label(r['market'], r['sport'])} · {r['pct']:.2f}% · "
                         f"do {money(r['cap'], '$')} $")
    lines += ["", "⚠️ Ovo je <b>gornja granica</b>: računa da je svaka uhvaćena. U stvarnosti deo nestane pre uplate, "
                  "Polymarket knjiga se pomeri, a novac je zauzet dok se meč ne završi. Pravi procenat pokazuju "
                  "📒 Tiketi posle par dana malih uplata, ili /bottest sa novcem upisanim na SX Bet + Polymarket."]
    return "\n".join(lines)


def tickets_text(trader: AutoTrader, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    runs = trader.book.runs(uid, limit=12)
    b = InlineKeyboardBuilder()
    if not runs:
        b.button(text="⬅️ Nazad", callback_data="au:back")
        return "📒 <b>Tiketi</b>\n\nJoš nema nijedne prave uplate.", b.as_markup()
    lines = ["📒 <b>Tiketi</b> (poslednjih 12):", ""]
    for r in runs:
        res = (f"{signed(r['profit'] or 0, '$')} $" if r["status"] == bk.SETTLED else
               f"čeka {signed(r['planned_profit'], '$')} $" if r["status"] == bk.OPEN else "")
        icon = {bk.OPEN: "⏳", bk.EXPOSED: "🚨", bk.FAILED: "❌", bk.SETTLED: "🏁"}.get(r["status"], "•")
        lines.append(f"{icon} #{r['id']} {_when(r['at'])} · {_esc(r['name'][:32])} · {money(r['total'], '$')} $ {res}")
        if r["status"] in (bk.OPEN, bk.EXPOSED):
            b.button(text=f"{icon} #{r['id']}", callback_data=f"au:tk:{r['id']}")
    b.button(text="⬅️ Nazad", callback_data="au:back")
    n = sum(1 for r in runs if r["status"] in (bk.OPEN, bk.EXPOSED))
    b.adjust(*([4] * (n // 4)), *([n % 4] if n % 4 else []), 1)
    lines += ["", "Klik na otvoren tiket: detalji i ručno zatvaranje (ako bot ne može sam da pročita ishod)."]
    return "\n".join(lines), b.as_markup()


def markup_for(spec, trader: AutoTrader) -> InlineKeyboardMarkup | None:
    """Buttons under the trader's messages: spec = (kind, id) from arb.live.engine."""
    if not spec:
        return None
    kind, ident = spec
    b = InlineKeyboardBuilder()
    if kind == "confirm":
        b.button(text="✅ Uplati", callback_data=f"au:go:{ident}")
        b.button(text="❌ Preskoči", callback_data=f"au:skip:{ident}")
        b.button(text="🛑 STOP", callback_data="au:stop")
        b.adjust(2, 1)
    elif kind in ("exposed", "unconfirmed"):
        b.button(text="🛟 Pokrij (gubitak do 10%)", callback_data=f"au:cover:{ident}")
        if kind == "unconfirmed":  # only when SX never confirmed the bet
            b.button(text="❌ SX Bet opklada nije prošla", callback_data=f"au:nop:{ident}")
        b.button(text="🛑 STOP", callback_data="au:stop")
        b.adjust(1)
    elif kind in ("ticket", "settle"):
        r = trader.book.run(ident)
        if r and r["status"] in (bk.OPEN, bk.EXPOSED) and kind == "settle":
            for bet in r["bets"]:
                if bet["status"] == bk.PLACED:
                    b.button(text=f"🏆 {bet['bookie']} dobio", callback_data=f"au:win:{ident}:{bet['id']}")
            b.button(text="↩️ Poništeno (ulozi vraćeni)", callback_data=f"au:win:{ident}:0")
            b.adjust(2, 1)
        else:
            b.button(text="📒 Tiketi", callback_data="au:tickets")
            b.button(text="🛑 STOP", callback_data="au:stop")
            b.adjust(2)
    return b.as_markup()


def make_sender(bot: Bot, trader_ref: list):
    """The trader's way to talk to the user (it doesn't know Telegram)."""
    async def send(uid: int, text: str, spec=None) -> None:
        try:
            await bot.send_message(uid, text, reply_markup=markup_for(spec, trader_ref[0]))
        except Exception:
            log.exception("auto message to %s failed", uid)
    return send


# ---------------------------------------------------------------- menu

@router.message(Command("bot", "auto"))
@router.message(F.text == kb.BTN_AUTO)
async def show_auto(m: Message, service, store: Storage, trader: AutoTrader) -> None:
    awaiting.pop(m.from_user.id, None)
    s = store.get(m.from_user.id)
    await m.answer(await auto_text(m.from_user.id, s, trader, service), reply_markup=auto_kb(s, trader))


# ---------------------------------------------------------------- typed values

def _awaiting_auto(m: Message) -> bool:
    v = awaiting.get(m.from_user.id)
    return isinstance(v, str) and v.startswith("au_")


async def _drop(m: Message) -> None:
    try:
        await m.delete()  # never leave a key in the chat
    except TelegramBadRequest:
        pass


@router.message(F.text, _awaiting_auto)
async def typed_auto(m: Message, service, store: Storage, trader: AutoTrader) -> None:
    uid = m.from_user.id
    what = awaiting[uid]
    s = store.get(uid)
    text = (m.text or "").strip()
    if what == "au_sxkey":
        await _drop(m)
        if not KEY_RE.match(text):
            await m.answer("❌ To nije privatni ključ (64 heksa znaka, sa ili bez 0x). Pošalji ponovo:")
            return
        awaiting.pop(uid, None)
        wait = await m.answer("⏳ Povezujem SX Bet…")
        ok, msg = await trader.sx.connect(key=text)
        text = None  # drop the key
        await wait.edit_text(("✅ SX Bet: " if ok else "❌ SX Bet: ") + msg)
        await m.answer(await auto_text(uid, s, trader, service), reply_markup=auto_kb(s, trader))
        return
    if what == "au_pmfunder":
        if not ADDRESS_RE.match(text):
            await m.answer("To nije adresa. Pošalji adresu Polymarket naloga: <code>0x</code> + 40 znakova "
                           "(polymarket.com → profil → kopiraj adresu).")
            return
        _pm_setup.setdefault(uid, {})["funder"] = text
        awaiting[uid] = "au_pmkey"
        await m.answer("🔑 Sad pošalji <b>privatni ključ</b> tog naloga kao običnu poruku.\n"
                       "Odmah brišem tvoju poruku; ključ ide u Windows Credential Manager, nikad u fajl.\n"
                       "Gde je: polymarket.com → Settings → Export Private Key (email/Google nalog), "
                       "ili MetaMask → Account details → Show private key.")
        return
    if what == "au_pmkey":
        await _drop(m)
        if not KEY_RE.match(text):
            await m.answer("❌ To nije privatni ključ (64 heksa znaka, sa ili bez 0x). Pošalji ponovo:")
            return
        awaiting.pop(uid, None)
        setup = _pm_setup.pop(uid, {})
        wait = await m.answer("⏳ Povezujem Polymarket…")
        ok, msg = await trader.poly.connect(setup.get("funder", ""), setup.get("sig", 1), key=text)
        text = None  # drop the key
        if ok:
            s.poly_funder, s.poly_sig = setup["funder"], setup.get("sig", 1)
            store.save()
        await wait.edit_text(("✅ Polymarket: " if ok else "❌ Polymarket: ") + msg)
        await m.answer(await auto_text(uid, s, trader, service), reply_markup=auto_kb(s, trader))
        return
    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(\$|%|h)?$", text)
    value = float(match.group(1).replace(",", ".")) if match else -1
    limits = {"au_cst": ("auto_stake", AUTO_MIN_STAKE, 10_000), "au_cdl": ("auto_daily", AUTO_MIN_STAKE, 100_000),
              "au_cop": ("auto_open", AUTO_MIN_STAKE, 100_000), "au_cmin": ("auto_min", 0.1, 50)}
    field, low, high = limits[what]
    if not low <= value <= high:
        await m.answer(f"Upiši broj između {low:g} i {high:g}.")
        return
    setattr(s, field, round(value, 2))
    awaiting.pop(uid, None)
    store.save()
    await m.answer(rules_text(s), reply_markup=rules_kb(s))


# ---------------------------------------------------------------- buttons

@router.callback_query(F.data.startswith("au:"))
async def cb_auto(c: CallbackQuery, service, store: Storage, trader: AutoTrader) -> None:
    uid = c.from_user.id
    s = store.get(uid)
    parts = c.data.split(":")
    action = parts[1]

    async def panel(note: str = "") -> None:
        await safe_edit(c, await auto_text(uid, s, trader, service), auto_kb(s, trader))
        await c.answer(note)

    if action == "back":
        awaiting.pop(uid, None)
        await panel()
    elif action == "toggle":
        if not s.auto and s.mode != "crypto":
            await c.answer("Prvo prebaci na 🪙 kripto (🏦 Kladionice → 🪙 Prebaci na kripto).", show_alert=True)
            return
        s.auto = not s.auto
        store.save()
        if s.auto:
            trader.halted = False
            missing = [x for x, ok in (("SX Bet (🔵)", await trader.ensure_sx()),
                                       ("Polymarket (🟣)", await trader.ensure_poly(s))) if not ok]
            note = ("🤖 Uključeno" + (" – ali fali: " + ", ".join(missing) if missing else
                                     ". Prve uplate posle sledećeg skeniranja."))
            await safe_edit(c, await auto_text(uid, s, trader, service), auto_kb(s, trader))
            await c.answer(note, show_alert=bool(missing))
        else:
            await panel("Isključeno – ništa novo se ne uplaćuje")
    elif action == "confirm":
        s.auto_confirm = not s.auto_confirm
        store.save()
        await panel("Pitaću te pre svake uplate" if s.auto_confirm else "Uplaćujem sam, bez pitanja")
    elif action == "stop":
        for u in store.users.values():
            u.auto = False
        store.save()
        await trader.stop()
        await c.answer("🛑 Zaustavljeno")
        await c.message.answer("🛑 <b>Sve je zaustavljeno.</b> Nijedna nova uplata ne ide dok ne uključiš 🤖 Auto ponovo. "
                               "Već uplaćeni tiketi ostaju – vidi 📒 Tiketi.", reply_markup=auto_kb(s, trader))
    elif action == "sx":
        b = InlineKeyboardBuilder()
        b.button(text="🔑 Pošalji privatni ključ", callback_data="au:sxkey")
        if trader.sx.connected or trader.sx.has_key():
            b.button(text="🗑 Odjavi i obriši ključ", callback_data="au:sxforget")
        b.button(text="⬅️ Nazad", callback_data="au:back")
        b.adjust(1)
        await safe_edit(c, "🔵 <b>Poveži SX Bet</b>\n\n"
                           "1. Na sx.bet uplati USDC na nalog (novac mora biti u <b>trading</b> balansu – napravi jednu "
                           "malu opkladu na sajtu da se nalog aktivira).\n"
                           "2. 🔑 SX API ključ (sx.bet → Settings → API) – ako ga još nisi uneo.\n"
                           "3. Pošalji <b>privatni ključ novčanika</b> tog naloga (sx.bet → Settings → Export private key, "
                           "ili MetaMask → Account details). Njime bot potpisuje naloge; ide u Windows Credential "
                           "Manager, nikad u fajl, a tvoju poruku odmah brišem.\n\n"
                           "💡 Napravi poseban novčanik samo za bota i drži na njemu samo novac za klađenje.",
                        b.as_markup())
        await c.answer()
    elif action == "sxkey":
        if not secrets.get("sxbet_api_key"):
            await c.answer("Prvo unesi 🔑 SX API ključ.", show_alert=True)
            return
        awaiting[uid] = "au_sxkey"
        await c.message.answer("🔑 Pošalji <b>privatni ključ</b> SX Bet novčanika kao običnu poruku (odmah je brišem):")
        await c.answer()
    elif action == "sxforget":
        trader.sx.forget()
        await panel("SX Bet odjavljen, ključ obrisan")
    elif action == "pm":
        b = InlineKeyboardBuilder()
        b.button(text="📧 Email / Google nalog", callback_data="au:pmsig:1")
        b.button(text="🦊 MetaMask / novčanik", callback_data="au:pmsig:2")
        if trader.poly.connected or trader.poly.has_key():
            b.button(text="🗑 Odjavi i obriši ključ", callback_data="au:pmforget")
        b.button(text="⬅️ Nazad", callback_data="au:back")
        b.adjust(1)
        await safe_edit(c, "🟣 <b>Poveži Polymarket</b>\n\nKako si napravio Polymarket nalog?", b.as_markup())
        await c.answer()
    elif action == "pmsig":
        _pm_setup[uid] = {"sig": int(parts[2])}
        awaiting[uid] = "au_pmfunder"
        await c.message.answer("📮 Pošalji <b>adresu Polymarket naloga</b> (0x…, polymarket.com → klik na profil → "
                               "kopiraj adresu; to je adresa na koju uplaćuješ USDC).")
        await c.answer()
    elif action == "pmforget":
        trader.poly.forget()
        s.poly_funder = ""
        store.save()
        await panel("Polymarket odjavljen, ključ obrisan")
    elif action == "bal":
        await c.answer("⏳")
        await trader.balances(s, force=True)
        await safe_edit(c, await auto_text(uid, s, trader, service), auto_kb(s, trader))
    elif action == "est":
        await c.answer()
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Osveži", callback_data="au:est")
        b.button(text="⬅️ Nazad", callback_data="au:back")
        await safe_edit(c, await estimate_text(uid, s, trader), b.as_markup())
    elif action == "rules":
        await safe_edit(c, rules_text(s), rules_kb(s))
        await c.answer()
    elif action in ("st", "dl", "op", "min", "h"):
        field = {"st": "auto_stake", "dl": "auto_daily", "op": "auto_open", "min": "auto_min", "h": "auto_hours"}[action]
        setattr(s, field, int(parts[2]) if action == "h" else float(parts[2]))
        store.save()
        await safe_edit(c, rules_text(s), rules_kb(s))
        await c.answer("Sačuvano")
    elif action in ("cst", "cdl", "cop", "cmin"):
        awaiting[uid] = "au_" + action
        await c.message.answer({"cst": "✏️ Najviše $ po arbitraži (npr. <code>4</code>):",
                                "cdl": "✏️ Najviše $ dnevno (npr. <code>40</code>):",
                                "cop": "✏️ Najviše $ u otvorenim tiketima (npr. <code>30</code>):",
                                "cmin": "✏️ Najmanji profit u % (npr. <code>0.8</code>):"}[action])
        await c.answer()
    elif action == "tickets":
        text, markup = tickets_text(trader, uid)
        await safe_edit(c, text, markup)
        await c.answer()
    elif action == "tk":
        r = trader.book.run(int(parts[2]))
        if not r or r["uid"] != uid:
            await c.answer("Nema tog tiketa")
            return
        spec = (("unconfirmed" if r["note"] == UNCONFIRMED else "exposed", r["id"]) if r["status"] == bk.EXPOSED
                else ("settle", r["id"]))
        await c.message.answer(ticket_text(r), reply_markup=markup_for(spec, trader))
        if r["status"] == bk.EXPOSED:
            await c.message.answer("Kad se meč završi, zatvori ga ovde:",
                                   reply_markup=markup_for(("settle", r["id"]), trader))
        await c.answer()
    elif action == "report":
        await c.answer()
        day = trader.book.day(uid, trader.day_start())
        await c.message.answer(report_text(day, await trader.balances(s)))
    elif action == "dry":
        await c.answer("⏳ Proba…")
        await _dry_run(c.message, uid, s, service, trader)
    elif action == "go":
        await c.answer("⏳ Proveravam kvote i uplaćujem…")
        await safe_edit(c, (c.message.html_text or "") + "\n\n⏳ <b>Uplaćujem…</b>")
        text, spec = await trader.confirm(uid, parts[2])
        await c.message.answer(text, reply_markup=markup_for(spec, trader))
    elif action == "skip":
        trader.pending.pop(parts[2], None)
        await safe_edit(c, (c.message.html_text or "") + "\n\n❌ Preskočeno.")
        await c.answer("Preskočeno")
    elif action == "cover":
        await c.answer("⏳ Pokušavam da pokrijem…")
        text, spec = await trader.cover(uid, int(parts[2]))
        await c.message.answer(text, reply_markup=markup_for(spec, trader))
    elif action == "nop":
        await c.answer()
        await c.message.answer(trader.not_placed(uid, int(parts[2])))
    elif action == "win":
        bet = int(parts[3])
        await c.answer()
        await safe_edit(c, await trader.settle_by_hand(uid, int(parts[2]), bet or None))
    else:
        await c.answer()


async def _dry_run(msg: Message, uid: int, s: UserSettings, service, trader: AutoTrader) -> None:
    """🔍 the whole path on the best arb right now: both orders built and signed, nothing sent."""
    if s.mode != "crypto":
        await msg.answer("🔍 Proba radi u 🪙 kripto režimu (🏦 Kladionice → 🪙 Prebaci na kripto).")
        return
    await service.fresh(s)
    arbs = [a for a in service.arbs_for(auto_view(s)) if is_pair(a)]
    if not arbs:
        await msg.answer("🔍 Trenutno nema nijedne arbitraže SX Bet + Polymarket. Proba radi kad neka postoji "
                         "(📈 Procena pokazuje koliko ih ima tokom dana).")
        return
    a = next((x for x in arbs if auto_ok(x, s) is None), arbs[0])
    text, _ = await trader.execute(uid, s, a, dry=True)
    await msg.answer(text or "🔍 Ništa za prikaz.")


def _esc(x) -> str:
    from html import escape

    return escape(str(x))
