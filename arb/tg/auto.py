"""🤖 Auto: real automatic betting on 1xBit + Polymarket (arb.live) - the accounts (log in,
connect), balances, tickets, rules, 🛑 Stop. Opened with the 🤖 Auto button (crypto mode),
/auto, or 🏦 Kladionice → 🤖 Nalozi."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from arb.live import book as bk
from arb.live.engine import (AUTO_MIN_STAKE, PAIR, UNCONFIRMED, AutoTrader, auto_ok, auto_view, report_text,
                             ticket_text)
from arb.live.polymarket import ADDRESS_RE, KEY_RE
from arb.tg import keyboards as kb
from arb.tg.formatting import TZ, money, signed
from arb.tg.handlers import allowed, awaiting, safe_edit
from arb.tg.storage import Storage, UserSettings

log = logging.getLogger(__name__)

router = Router()
router.message.filter(lambda m: m.from_user and allowed(m.from_user.id))
router.callback_query.filter(lambda c: allowed(c.from_user.id))

STAKES = [1, 2, 3, 5, 10]
DAILY = [10, 25, 50, 100]
OPEN = [10, 25, 50, 100]
MINS = [0.5, 1.0, 1.5, 2.0, 3.0]
HOURS = [1, 3, 6, 12, 24, 0]
_teach_task: dict[str, asyncio.Task] = {}
_pm_setup: dict[int, dict] = {}  # uid -> {"sig": 1/2, "funder": "0x..."} while connecting Polymarket


# ---------------------------------------------------------------- texts

def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m. %H:%M") if ts else "—"


async def auto_text(uid: int, s: UserSettings, trader: AutoTrader, service=None) -> str:
    on = "✅ <b>uključeno</b>" if s.auto else "⬜ <b>isključeno</b>"
    lines = [f"🤖 <b>Automatsko klađenje · 1xBit + Polymarket</b>: {on}"]
    if trader.halted:
        lines.append("🛑 <b>Zaustavljeno dugmetom STOP</b> – ništa novo ne ide dok ne uključiš ponovo.")
    lines.append("✋ Pre svake uplate te pita (dugme ✅ Uplati)" if s.auto_confirm
                 else "💸 Uplaćuje samo, bez pitanja")
    lines += ["", "<b>🏦 Nalozi</b>"]
    if await trader.ensure_poly(s):
        bal = await trader.poly.balance()
        kind = "email/Google" if s.poly_sig == 1 else "MetaMask"
        lines.append(f"🟣 Polymarket: ✅ povezan · {s.poly_funder[:6]}…{s.poly_funder[-4:]} ({kind}) · balans "
                     + (f"<b>{money(bal, '$')} $</b>" if bal is not None else f"? ({trader.poly.error})"))
    else:
        lines.append("🟣 Polymarket: ⬜ nije povezan – klikni 🟣 Poveži Polymarket")
    t = trader.onexbit.template()
    x = trader.book.balance(uid, "1xBit")
    learned = f"🎓 naučeno {_when(t.get('learned_at'))}" if t else "⬜ uplata nije naučena (🎓)"
    window = "prozor otvoren" if trader.onexbit.window_open else "prozor zatvoren"
    lines.append(f"🔵 1xBit: {learned} · {window}")
    if x.set_amount is None:
        lines.append("   balans: ⬜ nije upisan – klikni ✏️ 1xBit balans")
    else:
        lines.append(f"   balans ~<b>{money(x.now, '$')} $</b> (upisano {money(x.set_amount, '$')} $ {_when(x.set_at)}; "
                     f"posle toga uloženo {money(x.staked_since, '$')} $, vraćeno {money(x.returned_since, '$')} $)")
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
        n = sum(1 for a in service.arbs_for(auto_view(s)) if auto_ok(a, s) is None)
        lines.append(f"🔎 Trenutno prolazi: <b>{n}</b> arbitraža (1xBit + Polymarket)")
    if s.mode != "crypto":
        lines.append("\n⚠️ Radi samo u 🪙 kripto režimu (🏦 Kladionice → 🪙 Prebaci na kripto).")
    lines += ["", "Redosled: prvo 1xBit (može da odbije tiket – tada se ništa ne gubi), pa Polymarket "
                  "„sve ili ništa“ po ceni na kojoj je arbitraža najgore na nuli. Pre svake uplate: provera kvota, "
                  "limita, balansa i da li Polymarket dozvoljava tvoju zemlju."]
    return "\n".join(lines)


def auto_kb(s: UserSettings, trader: AutoTrader) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Auto: ✅ uključeno (klik = isključi)" if s.auto else "🤖 Auto: ⬜ isključeno (klik = uključi)",
             callback_data="au:toggle")
    b.button(text="✋ Pitaj pre uplate: ✅ da" if s.auto_confirm else "✋ Pitaj pre uplate: ⬜ ne (sam uplaćuje)",
             callback_data="au:confirm")
    b.button(text="🟣 Polymarket ✅ (promeni)" if trader.poly.connected else "🟣 Poveži Polymarket",
             callback_data="au:pm")
    b.button(text="🔄 Osveži balans", callback_data="au:bal")
    b.button(text="🔵 1xBit prijava (prozor)", callback_data="au:1xlogin")
    b.button(text="🎓 Nauči 1xBit uplatu" + (" ✅" if trader.onexbit.template() else ""), callback_data="au:1xteach")
    b.button(text="✏️ 1xBit balans", callback_data="au:1xbal")
    b.button(text="⚙️ Pravila", callback_data="au:rules")
    b.button(text="📒 Tiketi", callback_data="au:tickets")
    b.button(text="📊 Izveštaj", callback_data="au:report")
    b.button(text="🔍 Proba bez uplate", callback_data="au:dry")
    b.button(text="🛑 STOP – zaustavi sve", callback_data="au:stop")
    b.adjust(1, 1, 2, 2, 1, 3, 1, 1)
    return b.as_markup()


def rules_text(s: UserSettings) -> str:
    when = f"u narednih <b>{s.auto_hours} h</b>" if s.auto_hours else "<b>bilo kad</b>"
    return "\n".join([
        "⚙️ <b>Pravila za prave uplate</b>", "",
        f"💵 najviše <b>{money(s.auto_stake, '$')} $</b> po arbitraži (ukupno na obe strane)",
        f"📅 najviše <b>{money(s.auto_daily, '$')} $</b> uloženo dnevno",
        f"⏳ najviše <b>{money(s.auto_open, '$')} $</b> u tiketima čiji meč još nije završen",
        f"📈 profit najmanje <b>{s.auto_min:g}%</b>",
        f"⏰ meč počinje {when}",
        "", f"💡 Kreni sa malim ulozima (1–5 $). Najmanje {AUTO_MIN_STAKE:g} $ ukupno (Polymarket prima od 1 $).",
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
        lines.append(f"{icon} #{r['id']} {_when(r['at'])} · {r['name'][:32]} · {money(r['total'], '$')} $ {res}")
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
        if kind == "unconfirmed":  # only when 1xBit never confirmed the ticket
            b.button(text="❌ 1xBit tiket nije prošao", callback_data=f"au:nop:{ident}")
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

@router.message(Command("auto"))
@router.message(F.text == kb.BTN_AUTO)
async def show_auto(m: Message, service, store: Storage, trader: AutoTrader) -> None:
    awaiting.pop(m.from_user.id, None)
    s = store.get(m.from_user.id)
    await m.answer(await auto_text(m.from_user.id, s, trader, service), reply_markup=auto_kb(s, trader))


# ---------------------------------------------------------------- typed values

def _awaiting_auto(m: Message) -> bool:
    v = awaiting.get(m.from_user.id)
    return isinstance(v, str) and v.startswith("au_")


@router.message(F.text, _awaiting_auto)
async def typed_auto(m: Message, service, store: Storage, trader: AutoTrader) -> None:
    uid = m.from_user.id
    what = awaiting[uid]
    s = store.get(uid)
    text = (m.text or "").strip()
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
        try:
            await m.delete()  # never leave the key in the chat
        except TelegramBadRequest:
            pass
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
    if what == "au_1xbal":
        if not 0 <= value <= 1_000_000:
            await m.answer("Upiši koliko imaš na 1xBit-u u $ (USDT), npr. <code>25</code>")
            return
        trader.book.set_balance(uid, "1xBit", round(value, 2))
        awaiting.pop(uid, None)
        await m.answer(f"✅ 1xBit balans: <b>{money(value, '$')} $</b>. Od sad ga bot sam prati "
                       "(− ulog kad uplati, + dobitak kad se meč završi).")
        await m.answer(await auto_text(uid, s, trader, service), reply_markup=auto_kb(s, trader))
        return
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
        s.auto = not s.auto
        store.save()
        if s.auto:
            trader.halted = False
            missing = [x for x, ok in (("Polymarket", await trader.ensure_poly(s)),
                                       ("1xBit uplata (🎓)", bool(trader.onexbit.template())),
                                       ("1xBit balans (✏️)", trader.book.balance(uid, "1xBit").set_amount is not None))
                       if not ok]
            if trader.onexbit.template() and not trader.onexbit.window_open:
                asyncio.create_task(trader.onexbit.open_window())  # the session has to be open to bet
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
        await c.answer("🛑 Zaustavljeno", show_alert=False)
        await c.message.answer("🛑 <b>Sve je zaustavljeno.</b> Nijedna nova uplata ne ide dok ne uključiš 🤖 Auto ponovo. "
                               "Već uplaćeni tiketi ostaju – vidi 📒 Tiketi.", reply_markup=auto_kb(s, trader))
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
        if await trader.ensure_poly(s):
            await trader.poly.balance(force=True)
        await safe_edit(c, await auto_text(uid, s, trader, service), auto_kb(s, trader))
    elif action == "1xlogin":
        await c.answer("⏳ Otvaram 1xBit prozor…")
        res = await trader.onexbit.open_window()
        await c.message.answer("🔵 Otvorio sam <b>1xBit prozor</b> na računaru gde radi bot. Uloguj se tamo "
                               "(captcha / kod idu ručno). Prijava ostaje sačuvana za sledeći put.\n"
                               "Prozor ostavi otvoren (može minimizovan) dok bot radi automatski."
                               if res == "ok" else res)
    elif action == "1xteach":
        task = _teach_task.get("1xBit")
        if task and not task.done():
            await c.answer("Već čekam tvoju uplatu u 1xBit prozoru.", show_alert=True)
            return
        await c.answer()
        await c.message.answer(
            "🎓 <b>Učim 1xBit uplatu</b>\n\n"
            "1. U 1xBit prozoru: podešavanja → promena kvota → <b>„Ne prihvataj promene“</b> (bitno!)\n"
            "2. Izaberi bilo koji meč koji <b>još nije počeo</b> i klikni jednu kvotu (samo JEDAN par na tiketu)\n"
            "3. Upiši najmanji ulog i klikni <b>Uplati</b>\n\n"
            "Ja gledam šta sajt šalje i to zapamtim (u Windows Credential Manager). Imaš 10 minuta. "
            "Taj tiket je pravi – ne ulazi u knjigu.")
        _teach_task["1xBit"] = asyncio.create_task(_teach(c.message, trader))
    elif action == "1xbal":
        awaiting[uid] = "au_1xbal"
        x = trader.book.balance(uid, "1xBit")
        now = f" (bot misli ~{money(x.now, '$')} $)" if x.set_amount is not None else ""
        await c.message.answer(f"✏️ Koliko sad imaš na 1xBit-u u $ (USDT){now}? Upiši broj, npr. <code>25</code>")
        await c.answer()
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
            await c.message.answer("Kad se meč završi, zatvori ga ovde:", reply_markup=markup_for(("settle", r["id"]), trader))
        await c.answer()
    elif action == "report":
        await c.answer()
        day = trader.book.day(uid, trader.day_start())
        pm = await trader.poly.balance() if await trader.ensure_poly(s) else None
        await c.message.answer(report_text(day, trader.book.balance(uid, "1xBit"), pm))
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


async def _teach(msg: Message, trader: AutoTrader) -> None:
    try:
        ok, info = await trader.onexbit.teach()
    except Exception as e:
        log.exception("1xBit teach failed")
        ok, info = False, str(e).splitlines()[0][:200] if str(e) else type(e).__name__
    await msg.answer(("✅ <b>1xBit uplata naučena</b>: " if ok else "❌ <b>Nisam naučio</b>: ") + _esc(info)
                     + ("\n\nUpiši ✏️ 1xBit balans (koliko je ostalo posle tog tiketa) i probaj 🔍 Proba bez uplate."
                        if ok else ""))


async def _dry_run(msg: Message, uid: int, s: UserSettings, service, trader: AutoTrader) -> None:
    """🔍 the whole path on the best arb right now, without sending anything."""
    await service.fresh(s)
    arbs = [a for a in service.arbs_for(auto_view(s)) if {l.bookie for l in a.legs} == set(PAIR)]
    if not arbs:
        await msg.answer("🔍 Trenutno nema nijedne arbitraže 1xBit + Polymarket. Proba radi kad neka postoji.")
        return
    a = next((x for x in arbs if auto_ok(x, s) is None), arbs[0])
    text, _ = await trader.execute(uid, s, a, dry=True)
    await msg.answer(text or "🔍 Ništa za prikaz.")


def _esc(x) -> str:
    from html import escape

    return escape(str(x))
