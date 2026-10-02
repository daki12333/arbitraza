"""📒 Tiketi i balans (/tiketi, the 📒 menu button): the user plays by hand on any bookie, the bot
only keeps track - money on every bookie, tickets in play, results, stats. Nothing is placed.

A ticket comes from "✍️ Odigrao sam" under an arb (stakes and odds as shown, editable before saving)
or is typed in by hand. When the match is surely over the bot asks which leg went through and books
the payouts. Money: ➕ uplata, ➖ isplata, 🔁 prebaci (between bookies, fees included), ✏️ ispravi
(the bookie shows another balance). Polymarket / SX Bet balances can also be read from their API
when they are connected in /bot. The book is arb.track (data/tracker.db)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from arb import track
from arb.models import SPORT_ICONS, market_label
from arb.tg import keyboards as kb
from arb.tg.formatting import TZ, fmt_odd, money, outcome_text, signed
from arb.tg.handlers import allowed, awaiting, safe_edit
from arb.tg.storage import ALL_BOOKIES, BOOKIE_REGION, CURRENCY, Storage, UserSettings

log = logging.getLogger(__name__)

router = Router()
router.message.filter(lambda m: m.from_user and allowed(m.from_user.id))
router.callback_query.filter(lambda c: allowed(c.from_user.id))

book = track.Tracker()
REMIND_EVERY = 120  # s between checks for tickets whose match is over
OPEN_SHOWN = 30
DONE_SHOWN = 15
STATS_DAYS = [7, 30, 0]  # 0 = all time
MENU = (kb.BTN_LIST, kb.BTN_ARBS, kb.BTN_BUDGET, kb.BTN_BOOKIES, kb.BTN_STATUS, kb.BTN_AUTO, kb.BTN_NOTIFY,
        kb.BTN_MIDDLES, kb.BTN_TRACK, "🔕")
RESULT_NAMES = {"": "⏳ čeka", track.WON: "✅ prošao", track.LOST: "❌ pao", track.VOID: "↩️ vraćen ulog",
                track.HALF_WON: "🌗 pola prošlo", track.HALF_LOST: "🌘 pola palo"}
RESULT_CYCLE = ["", track.WON, track.LOST, track.VOID, track.HALF_WON, track.HALF_LOST]
MOVE_NAMES = {"deposit": "➕ uplata", "withdraw": "➖ isplata", "move_out": "🔁 poslato na {note}",
              "move_in": "🔁 stiglo sa {note}", "fix": "✏️ ispravka", "stake": "🎫 ulog #{tid}",
              "payout": "💵 dobitak #{tid}"}
MONEY_ASK = {"dep": "➕ Koliko si uplatio na <b>{b}</b>? Upiši iznos ({c}), npr. <code>{ex}</code>",
             "wd": "➖ Koliko si podigao sa <b>{b}</b>? Upiši iznos ({c}), npr. <code>{ex}</code>",
             "fix": "✏️ Koliko <b>{b}</b> sad stvarno pokazuje? Upiši iznos ({c}), npr. <code>{ex}</code>\n"
                    "Po evidenciji je {have} {c}; razlika se upisuje kao ispravka."}
MANUAL_HELP = (
    "✍️ <b>Ručni tiket</b>: upiši u jednoj poruci, prvi red je meč (a na kraju vreme početka, ako ga znaš), "
    "pa po red za svaku uplatu: <b>kladionica · ishod · kvota · ulog</b>\n\n"
    "<code>Real – Barcelona 20:45\nMozzart 1 2.10 5000\nMeridian X2 1.95 5200</code>\n\n"
    "Ishod piši kako hoćeš (<code>1</code>, <code>X2</code>, <code>više 2.5</code>…). Vreme može i sa datumom: "
    "<code>03.10. 20:45</code>. Bez vremena bot pita za rezultat posle 3 h.")


def cur_of(bookie: str) -> str:
    return CURRENCY.get(BOOKIE_REGION.get(bookie, "crypto"), "$")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m. %H:%M")


def _plain(text: str) -> str:
    """Button text: labels are stored HTML-escaped."""
    return html.unescape(text)


# ---------------------------------------------------------------- typed money / odds

MONEY_RE = re.compile(r"^\s*(\d{1,3}(?:[ .]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(k)?\s*"
                      r"(?:din|rsd|\$|usd|usdt|usdc)?\s*$", re.IGNORECASE)


def parse_money(text: str) -> float | None:
    """"5000", "5.000", "5 000", "5k", "1,5k", "10.5", "10,50 $" -> the amount (None = not a number)."""
    m = MONEY_RE.match(text or "")
    if not m:
        return None
    num, k = m.group(1).replace(" ", ""), m.group(2)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", num):  # 5.000 / 12.345,50 = thousands
        num = num.replace(".", "").replace(",", ".")
    num = num.replace(",", ".")
    try:
        x = float(num) * (1000 if k else 1)
    except ValueError:
        return None
    return round(x, 2)


def parse_odd(text: str) -> float | None:
    try:
        x = float(text.strip().lstrip("@").replace(",", "."))
    except ValueError:
        return None
    return x if 1.0 < x <= 1000 else None


def example(currency: str) -> str:
    return "50" if currency == "$" else "5000"


# ---------------------------------------------------------------- drafts (a ticket before ✅ Sačuvaj)

@dataclass
class Draft:
    name: str
    currency: str
    legs: list[dict]  # {"bookie", "outcome", "label", "odd", "stake"}
    start: float = 0.0
    sport: str = ""
    market: str = ""
    arb_key: str = ""
    msg: tuple[int, int] | None = None  # (chat, message) showing it
    created: float = field(default_factory=time.time)

    @property
    def total(self) -> float:
        return sum(l["stake"] for l in self.legs)

    @property
    def planned(self) -> float:
        """Profit if the worst leg goes through (an arb: the same on every leg)."""
        if len(self.legs) < 2:
            return 0.0
        return min(l["stake"] * l["odd"] for l in self.legs) - self.total


drafts: dict[int, Draft] = {}


def draft_from_arb(arb, key: str, budget: float, currency: str) -> Draft:
    rows = arb.plan(budget, currency) or arb.rounded_stakes(budget, currency)
    ev = arb.event
    legs = [{"bookie": leg.bookie, "outcome": leg.outcome, "label": outcome_text(arb, leg.outcome),
             "odd": round(pay / stake, 3) if stake else leg.odd, "stake": stake}
            for leg, stake, pay in rows if stake > 0]
    return Draft(name=f"{html.escape(ev.home)} – {html.escape(ev.away)}", currency=currency, legs=legs,
                 start=ev.start.timestamp(), sport=ev.sport,
                 market=market_label(arb.market, ev.sport, ev.home, ev.away), arb_key=key)


def match_bookie(line: str) -> tuple[str, str] | None:
    """("Mozzart", rest of the line) - the longest bookie name the line starts with."""
    low = line.lower()
    for name in sorted(ALL_BOOKIES, key=len, reverse=True):
        if low.startswith(name.lower()):
            return name, line[len(name):]
    return None


START_RE = re.compile(r"\s+(?:(\d{1,2})\.(\d{1,2})\.?\s+)?(\d{1,2})[:.](\d{2})\s*$")


def parse_start(name: str, now: datetime | None = None) -> tuple[str, float | None]:
    """"Real – Barca 20:45" -> ("Real – Barca", today 20:45 / tomorrow if that was hours ago)."""
    now = now or datetime.now(TZ)
    m = START_RE.search(" " + name)
    if not m:
        return name.strip(), None
    day, month, hour, minute = m.groups()
    try:
        when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if day:
            when = when.replace(month=int(month), day=int(day))
        elif when < now - timedelta(hours=12):
            when += timedelta(days=1)
    except ValueError:
        return name.strip(), None
    return (" " + name)[:m.start()].strip(), when.timestamp()


def parse_manual(text: str) -> tuple[Draft | None, str]:
    """A typed ticket (see MANUAL_HELP) -> (draft, "") or (None, what's wrong)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return None, "Treba bar dva reda: meč, pa bar jedna uplata."
    name, start = parse_start(lines[0])
    legs = []
    for line in lines[1:]:
        hit = match_bookie(line)
        if not hit:
            return None, f"Ne znam kladionicu u redu „{html.escape(line)}“. Piši je kao u 🏦 Kladionice (npr. Mozzart, 1xBit, Polymarket)."
        bookie, rest = hit
        parts = rest.split()
        if len(parts) < 3:
            return None, f"U redu „{html.escape(line)}“ fali nešto: kladionica · ishod · kvota · ulog."
        odd, stake = parse_odd(parts[-2]), parse_money(parts[-1])
        if odd is None or not stake:
            return None, f"U redu „{html.escape(line)}“ ne razumem kvotu ili ulog (kvota pa ulog, npr. <code>2.10 5000</code>)."
        outcome = " ".join(parts[:-2])
        legs.append({"bookie": bookie, "outcome": outcome, "label": html.escape(outcome), "odd": odd, "stake": stake})
    currencies = {cur_of(l["bookie"]) for l in legs}
    if len(currencies) > 1:
        return None, "Jedan tiket ne može da meša dinare i $ (srpske i kripto kladionice) – unesi ih posebno."
    return Draft(name=html.escape(name) or "Tiket", currency=currencies.pop(), legs=legs,
                 start=start or time.time()), ""


def draft_text(d: Draft, uid: int) -> str:
    c = d.currency
    have = book.balances(uid)
    head = f"{SPORT_ICONS.get(d.sport, '🎫')} <b>{d.name}</b>"
    sub = " · ".join(x for x in (d.market, _when(d.start) if d.start else "") if x)
    lines = ["✍️ <b>Novi tiket</b> – proveri pa ✅ Sačuvaj", "", head] + ([sub] if sub else [])
    for i, l in enumerate(d.legs, 1):
        line = f"{i}. <b>{l['bookie']}</b>: {l['label']} @ {fmt_odd(l['odd'])} → <b>{money(l['stake'], c)}</b> {c}"
        if l["bookie"] in have and have[l["bookie"]] < l["stake"] - 1e-9:
            line += f"\n   ⚠️ po evidenciji tamo imaš {money(have[l['bookie']], c)} {c}"
        lines.append(line)
    lines.append(f"\nUkupno <b>{money(d.total, c)}</b> {c}")
    if len(d.legs) > 1:
        for l in d.legs:
            pay = l["stake"] * l["odd"]
            lines.append(f"• ako prođe {l['bookie']}: {money(pay, c)} ({signed(pay - d.total, c)})")
        pct = d.planned / d.total * 100 if d.total else 0
        lines.append(f"Sigurno: <b>{signed(d.planned, c)} {c}</b> ({f'{pct:.2f}'.replace('.', ',')} %)")
    lines.append("\nUplatio si drugačije? Klikni nogu i upiši ulog i kvotu (npr. "
                 f"<code>{example(c)} 2.05</code>), samo ulog, ili <code>0</code> da je skloniš.")
    return "\n".join(lines)


def draft_kb(d: Draft) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Sačuvaj tiket", callback_data="tr:dsave")
    for i, l in enumerate(d.legs):
        b.button(text=f"✏️ {l['bookie']}: {_plain(l['label'])[:24]} · {money(l['stake'], d.currency)}",
                 callback_data=f"tr:dleg:{i}")
    b.button(text="❌ Odustani", callback_data="tr:dcancel")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------- texts

async def api_balances(trader) -> dict[str, float]:
    """Polymarket / SX Bet balance from their API, when connected in /bot."""
    out: dict[str, float] = {}
    if trader is None:
        return out
    for name, acc in (("SX Bet", getattr(trader, "sx", None)), ("Polymarket", getattr(trader, "poly", None))):
        try:
            if acc is not None and acc.connected:
                v = await asyncio.wait_for(acc.balance(), 10)
                if v is not None:
                    out[name] = v
        except Exception as e:
            log.warning("%s balance for 📒 failed: %s", name, e)
    return out


def _totals(rows: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for b, v in rows.items():
        out[cur_of(b)] = out.get(cur_of(b), 0.0) + v
    return out


def home_text(uid: int, s: UserSettings, api: dict[str, float] | None = None) -> str:
    bal, play = book.balances(uid), book.in_play(uid)
    api = api or {}
    names = sorted(set(bal) | set(play) | set(api), key=lambda b: (cur_of(b) != s.currency, b.lower()))
    lines = ["📒 <b>Tiketi i balans</b> · bot ništa ne uplaćuje, samo prati šta ti igraš", ""]
    if not names:
        return "\n".join(lines + [
            "Još ništa nije upisano. Kako ide:",
            "1. ➕ <b>Uplata</b>: upiši koliko imaš na kojoj kladionici",
            "2. kod arbitraže klikni ✍️ <b>Odigrao sam</b> (ili ✍️ Ručni tiket) – ulozi idu sa balansa",
            "3. kad se meč završi bot pita ko je prošao i dobitak vraća na tu kladionicu",
            "4. 🔁 <b>Prebaci</b> kad šalješ novac sa jedne kladionice na drugu, 📊 Statistika za sve"])
    lines.append("<b>💰 Balans</b> (slobodno · u igri)")
    for b in names:
        c = cur_of(b)
        line = f"• {b}: <b>{money(bal.get(b, 0.0), c)}</b> {c}"
        if play.get(b):
            line += f" · u igri {money(play[b], c)}"
        if b in api:
            same = abs(api[b] - bal.get(b, 0.0)) < 0.01
            line += f" · API {money(api[b], c)} {'✅' if same else '⚠️'}"
        lines.append(line)
    free, held = _totals(bal), _totals(play)
    lines.append("Ukupno (sa onim u igri): " + " · ".join(
        f"<b>{money(free.get(c, 0.0) + held.get(c, 0.0), c)} {c}</b>" for c in sorted(set(free) | set(held))))
    n_open = len(book.tickets(uid))
    waiting = sum(1 for t in book.tickets(uid) if t["start"] <= time.time() - track.FINISH_AFTER)
    lines.append(f"\n<b>📋 Otvoreni tiketi</b>: {n_open}" + (f" · ⏰ čeka rezultat: <b>{waiting}</b>" if waiting else ""))
    st = book.stats(uid, time.time() - 30 * 86400)
    done = [f"{signed(o['profit'], c)} {c} ({o['tickets']})" for c, o in st.items() if o["tickets"]]
    if done:
        lines.append("<b>📊 Poslednjih 30 dana</b>: " + " · ".join(done))
    return "\n".join(lines)


def home_kb(uid: int, api_ok: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"📋 Otvoreni tiketi ({len(book.tickets(uid))})", callback_data="tr:open")
    b.button(text="✅ Završeni", callback_data="tr:done")
    b.button(text="📊 Statistika", callback_data="tr:stats:30")
    b.button(text="➕ Uplata", callback_data="tr:pick:dep")
    b.button(text="➖ Isplata", callback_data="tr:pick:wd")
    b.button(text="🔁 Prebaci", callback_data="tr:pick:mv")
    b.button(text="✏️ Ispravi balans", callback_data="tr:pick:fix")
    b.button(text="🧾 Istorija novca", callback_data="tr:hist")
    b.button(text="✍️ Ručni tiket", callback_data="tr:manual")
    rows = [2, 1, 3, 2, 1]
    if api_ok:
        b.button(text="🔗 Uzmi balans sa Polymarket / SX API-ja", callback_data="tr:api")
        rows.append(1)
    b.button(text="🔄 Osveži", callback_data="tr:home")
    b.adjust(*rows, 1)
    return b.as_markup()


def ticket_text(t, legs, head: str = "") -> str:
    c = t["currency"]
    total = sum(l["stake"] for l in legs)
    lines = [head] if head else []
    lines.append(f"{SPORT_ICONS.get(t['sport'], '🎫')} <b>Tiket #{t['id']}</b> · {t['name']}")
    sub = [t["market"]] if t["market"] else []
    sub.append(("počinje " if t["start"] > time.time() else "počeo ") + _when(t["start"]))
    lines.append(" · ".join(sub))
    for l in legs:
        line = f"• <b>{l['bookie']}</b>: {l['label']} @ {fmt_odd(l['odd'])} → {money(l['stake'], c)} {c}"
        if l["result"]:
            line += f" · {RESULT_NAMES[l['result']]}"
            if t["status"] == track.SETTLED:
                line += f" ({money(l['payout'] or 0, c)})"
        lines.append(line)
    lines.append(f"Uloženo {money(total, c)} {c}" + (f" · planirano {signed(t['planned'], c)} {c}" if len(legs) > 1 else ""))
    if t["status"] == track.SETTLED:
        lines.append(f"🏁 <b>Završen: {signed(t['profit'], c)} {c}</b>")
    else:
        lines.append("⏳ Čeka meč" if t["start"] > time.time() - track.FINISH_AFTER else "⏰ <b>Ko je prošao?</b>")
    return "\n".join(lines)


def ticket_kb(t, legs, manual: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    tid = t["id"]
    rows = []
    if t["status"] == track.SETTLED:
        b.button(text="↩️ Poništi rezultat (pogrešno upisan)", callback_data=f"tr:undo:{tid}")
        rows.append(1)
    elif manual:
        for l in legs:
            b.button(text=f"{RESULT_NAMES[l['result']]} · {l['bookie']}: {_plain(l['label'])[:22]}",
                     callback_data=f"tr:cy:{tid}:{l['id']}")
            rows.append(1)
        b.button(text="💾 Zatvori tiket", callback_data=f"tr:close:{tid}")
        rows.append(1)
    else:
        for l in legs:
            b.button(text=f"✅ Prošao: {l['bookie']} · {_plain(l['label'])[:24]}", callback_data=f"tr:w:{tid}:{l['id']}")
            rows.append(1)
        b.button(text="✏️ Drugo (povraćaj, pola, sve palo…)", callback_data=f"tr:man:{tid}")
        b.button(text="🗑 Obriši tiket", callback_data=f"tr:del:{tid}")
        rows += [1, 1]
    b.button(text="⬅️ Tiketi", callback_data="tr:open" if t["status"] == track.OPEN else "tr:done")
    b.button(text="📒 Balans", callback_data="tr:home")
    b.adjust(*rows, 2)
    return b.as_markup()


def list_text(uid: int, status: str) -> tuple[str, InlineKeyboardMarkup]:
    rows = book.tickets(uid, status, OPEN_SHOWN if status == track.OPEN else DONE_SHOWN)
    b = InlineKeyboardBuilder()
    if status == track.OPEN:
        lines = [f"📋 <b>Otvoreni tiketi</b> ({len(rows)})", ""]
    else:
        lines = [f"✅ <b>Završeni tiketi</b> (poslednjih {len(rows)})", ""]
    if not rows:
        lines.append("Nema ih." if status == track.SETTLED else
                     "Nema otvorenih. Kod arbitraže klikni ✍️ Odigrao sam, ili ✍️ Ručni tiket.")
    for i, t in enumerate(rows, 1):
        legs = book.legs(t["id"])
        c = t["currency"]
        who = " + ".join(dict.fromkeys(l["bookie"] for l in legs))
        if status == track.OPEN:
            due = t["start"] <= time.time() - track.FINISH_AFTER
            lines.append(f"{i}. {'⏰ ' if due else ''}{_when(t['start'])} · {t['name']} · {who} · "
                         f"{money(sum(l['stake'] for l in legs), c)} {c}")
        else:
            lines.append(f"{i}. {'🟢' if t['profit'] >= 0 else '🔴'} {t['name']} · {who} · "
                         f"<b>{signed(t['profit'], c)} {c}</b>")
        b.button(text=str(i), callback_data=f"tr:t:{t['id']}")
    n = len(rows)
    b.button(text="📒 Balans", callback_data="tr:home")
    b.adjust(*([5] * (n // 5)), *([n % 5] if n % 5 else []), 1)
    return "\n".join(lines), b.as_markup()


def stats_text(uid: int, days: int) -> str:
    since = time.time() - days * 86400 if days else 0.0
    st = book.stats(uid, since)
    title = f"poslednjih {days} dana" if days else "od početka"
    lines = [f"📊 <b>Statistika</b> · {title}"]
    if not any(o["tickets"] or o["open"] for o in st.values()):
        return "\n".join(lines + ["", "Još nema završenih tiketa."])
    for c, o in st.items():
        lines += ["", f"<b>{'💵 dolari' if c == '$' else '🇷🇸 dinari'}</b>"]
        lines.append(f"Završeno tiketa: <b>{o['tickets']}</b> (🟢 {o['plus']} · 🔴 {o['minus']}) · otvoreno {o['open']}")
        if not o["tickets"]:
            continue
        roi = o["profit"] / o["staked"] * 100 if o["staked"] else 0
        lines.append(f"Promet: {money(o['staked'], c)} {c} · profit <b>{signed(o['profit'], c)} {c}</b> · "
                     f"ROI {f'{roi:.2f}'.replace('.', ',')} %")
        lines.append(f"Prosek po tiketu: {signed(o['profit'] / o['tickets'], c)} {c}")
        diff = o["profit"] - o["planned"]
        if abs(diff) >= (0.01 if c == "$" else 1):
            lines.append(f"Planirano {signed(o['planned'], c)} → razlika <b>{signed(diff, c)} {c}</b> "
                         "(povraćaji, promene kvota, greške)")
        if o["best"] is not None and o["tickets"] > 1:
            lines.append(f"Najbolji: #{o['best']['id']} {o['best']['name']} {signed(o['best']['profit'], c)} · "
                         f"najgori: #{o['worst']['id']} {o['worst']['name']} {signed(o['worst']['profit'], c)}")
        lines.append("<b>Po kladionici</b> (noge · promet · neto):")
        for b, v in sorted(o["bookies"].items(), key=lambda kv: -kv[1]["staked"]):
            lines.append(f"• {b}: {v['legs']} · {money(v['staked'], c)} · {signed(v['net'], c)}")
        lines.append("<b>Parovi</b> (tiketa · profit):")
        for p, v in sorted(o["pairs"].items(), key=lambda kv: -kv[1]["n"])[:8]:
            lines.append(f"• {p}: {v['n']} · {signed(v['profit'], c)}")
    return "\n".join(lines)


def stats_kb(days: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in STATS_DAYS:
        label = f"{d} dana" if d else "Sve"
        b.button(text=f"✅ {label}" if d == days else label, callback_data=f"tr:stats:{d}")
    b.button(text="📒 Balans", callback_data="tr:home")
    b.adjust(len(STATS_DAYS), 1)
    return b.as_markup()


def history_text(uid: int) -> str:
    rows = book.moves(uid, 25)
    lines = ["🧾 <b>Istorija novca</b> (poslednjih 25)", ""]
    if not rows:
        lines.append("Prazno.")
    for r in rows:
        c = cur_of(r["bookie"])
        what = MOVE_NAMES.get(r["kind"], r["kind"]).format(note=r["note"], tid=r["ticket_id"])
        lines.append(f"{_when(r['at'])} · {r['bookie']} · {what} <b>{signed(r['amount'], c)}</b> {c}")
    return "\n".join(lines)


def bookie_names(uid: int, s: UserSettings, skip: str = "") -> list[str]:
    """The user's bookies first (with money or in play), then the rest of the current mode's."""
    have = set(book.balances(uid)) | set(book.in_play(uid))
    mine = [b for b in ALL_BOOKIES if b in have]
    rest = [b for b in s.bookies if b not in have]
    if skip:  # 🔁 the second bookie: same currency only
        return [b for b in mine + [b for b in s.mode_bookies if b not in have] if b != skip and cur_of(b) == cur_of(skip)]
    return mine + rest


def pick_kb(names: list[str], action: str, bal: dict[str, float], back: str = "tr:home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in names:
        label = f"{n}: {money(bal[n], cur_of(n))}" if n in bal else n
        b.button(text=label, callback_data=f"tr:b:{action}:{n}")
    b.button(text="⬅️ Nazad", callback_data=back)
    b.adjust(*([2] * (len(names) // 2)), *([1] if len(names) % 2 else []), 1)
    return b.as_markup()


# ---------------------------------------------------------------- menu

async def _home(uid: int, s: UserSettings, trader=None) -> tuple[str, InlineKeyboardMarkup]:
    api = await api_balances(trader)
    return home_text(uid, s, api), home_kb(uid, bool(api))


@router.message(Command("tiketi", "balans"))
@router.message(F.text == kb.BTN_TRACK)
async def show_home(m: Message, store: Storage, trader=None) -> None:
    awaiting.pop(m.from_user.id, None)
    text, markup = await _home(m.from_user.id, store.get(m.from_user.id), trader)
    await m.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- typed values

def _awaiting_tr(m: Message) -> bool:
    v = awaiting.get(m.from_user.id)
    if not (isinstance(v, str) and v.startswith("tr:")):
        return False
    text = m.text or ""
    if text.startswith("/") or text.startswith(MENU):  # a menu button / command: leave what was asked
        awaiting.pop(m.from_user.id, None)
        return False
    return True


@router.message(F.text, _awaiting_tr)
async def typed(m: Message, bot: Bot, store: Storage) -> None:
    uid = m.from_user.id
    what = awaiting[uid]
    parts = what.split(":")
    s = store.get(uid)
    text = (m.text or "").strip()

    if parts[1] == "manual":
        d, err = parse_manual(text)
        if d is None:
            await m.answer(f"❌ {err}\n\nPošalji ponovo ili klikni ❌ u 📒.")
            return
        awaiting.pop(uid, None)
        drafts[uid] = d
        sent = await m.answer(draft_text(d, uid), reply_markup=draft_kb(d))
        d.msg = (sent.chat.id, sent.message_id)
        return

    if parts[1] == "dleg":
        d = drafts.get(uid)
        i = int(parts[2])
        if d is None or i >= len(d.legs):
            awaiting.pop(uid, None)
            await m.answer("Taj tiket više nije otvoren za izmenu.")
            return
        leg = d.legs[i]
        bits = text.replace("@", " @").split()
        stake = odd = None
        if len(bits) == 2:
            stake, odd = parse_money(bits[0]), parse_odd(bits[1])
            if stake is None or odd is None:
                await m.answer(f"Upiši ulog pa kvotu, npr. <code>{example(d.currency)} 2.05</code>")
                return
        elif len(bits) == 1 and bits[0].startswith("@"):
            odd = parse_odd(bits[0])
            if odd is None:
                await m.answer("Kvota mora biti veća od 1, npr. <code>@2.05</code>")
                return
        elif len(bits) == 1:
            stake = parse_money(bits[0])
            if stake is None:
                await m.answer(f"Upiši ulog (npr. <code>{example(d.currency)}</code>), ulog i kvotu, ili <code>0</code>.")
                return
        else:
            await m.answer(f"Upiši ulog pa kvotu, npr. <code>{example(d.currency)} 2.05</code>")
            return
        awaiting.pop(uid, None)
        if stake == 0:
            d.legs.pop(i)
        else:
            leg["stake"] = stake if stake is not None else leg["stake"]
            leg["odd"] = odd if odd is not None else leg["odd"]
        if not d.legs:
            drafts.pop(uid, None)
            await m.answer("Tiket je prazan – nije sačuvan.")
            return
        await _show_draft(bot, m, uid, d)
        return

    # money on one bookie: tr:dep:<b> / tr:wd:<b> / tr:fix:<b> / tr:mv:<src>:<dst>
    action, bookie = parts[1], parts[2]
    c = cur_of(bookie)
    if action == "mv":  # "100" or "100 98.5" (sent, arrived)
        bits = text.split()
        sent = parse_money(bits[0]) if bits else None
        got = parse_money(bits[1]) if len(bits) == 2 else sent
        if not sent or got is None or len(bits) > 2 or got > sent:
            await m.answer(f"Upiši koliko je poslato (i koliko je stiglo, ako su skinute naknade), npr. "
                           f"<code>{example(c)}</code> ili <code>{example(c)} {float(example(c)) * 0.98:g}</code>")
            return
        dst = parts[3]
        book.transfer(uid, bookie, dst, sent, got)
        fee = f" (naknada {money(sent - got, c)} {c})" if sent - got > 0 else ""
        note = f"✅ 🔁 {bookie} → {dst}: {money(sent, c)} {c}{fee}"
    else:
        amount = parse_money(text)
        if amount is None or (action != "fix" and amount <= 0):
            await m.answer(f"Upiši iznos ({c}), npr. <code>{example(c)}</code>")
            return
        if action == "dep":
            book.deposit(uid, bookie, amount)
            note = f"✅ ➕ {bookie}: +{money(amount, c)} {c}"
        elif action == "wd":
            book.withdraw(uid, bookie, amount)
            note = f"✅ ➖ {bookie}: -{money(amount, c)} {c}"
        else:
            diff = book.fix(uid, bookie, amount)
            note = (f"✅ ✏️ {bookie}: sad {money(amount, c)} {c} (razlika {signed(diff, c)})" if diff
                    else f"✅ {bookie}: evidencija se već slaže ({money(amount, c)} {c})")
    awaiting.pop(uid, None)
    await m.answer(note + "\n\n" + home_text(uid, s), reply_markup=home_kb(uid))


async def _show_draft(bot: Bot, m: Message, uid: int, d: Draft) -> None:
    if d.msg:
        try:
            await bot.edit_message_text(draft_text(d, uid), chat_id=d.msg[0], message_id=d.msg[1],
                                        reply_markup=draft_kb(d))
            await m.answer("✅ Izmenjeno ☝️")
            return
        except Exception:
            pass
    sent = await m.answer(draft_text(d, uid), reply_markup=draft_kb(d))
    d.msg = (sent.chat.id, sent.message_id)


# ---------------------------------------------------------------- "✍️ Odigrao sam" under an arb

@router.callback_query(F.data.startswith("tk:"))
async def cb_played(c: CallbackQuery, store: Storage) -> None:
    _, token, budget = c.data.split(":")
    s = store.get(c.from_user.id)
    shown = kb.SHOWN.get(int(token))
    arb, key = (shown[1], shown[0]) if shown else (None, "")
    if arb is None:
        await c.answer("Ta arbitraža je stara (bot je u međuvremenu restartovan). Unesi tiket ručno: 📒 → ✍️ Ručni tiket.",
                       show_alert=True)
        return
    d = draft_from_arb(arb, key, float(budget), s.currency)
    if not d.legs:
        await c.answer("Za ovaj ulog nema uloga po nogama – unesi tiket ručno.", show_alert=True)
        return
    drafts[c.from_user.id] = d
    await c.answer()
    sent = await c.message.answer(draft_text(d, c.from_user.id), reply_markup=draft_kb(d))
    d.msg = (sent.chat.id, sent.message_id)


# ---------------------------------------------------------------- buttons

@router.callback_query(F.data.startswith("tr:"))
async def cb(c: CallbackQuery, store: Storage, trader=None) -> None:
    uid = c.from_user.id
    s = store.get(uid)
    parts = c.data.split(":")
    action = parts[1]

    if action in ("home", "new"):
        awaiting.pop(uid, None)
        text, markup = await _home(uid, s, trader)
        if action == "new":
            await c.message.answer(text, reply_markup=markup)
        else:
            await safe_edit(c, text, markup)
        await c.answer()
        return

    if action == "api":  # book what Polymarket / SX Bet really show
        api = await api_balances(trader)
        changed = [f"{b}: {signed(book.fix(uid, b, v), '$')} $" for b, v in api.items()]
        await safe_edit(c, home_text(uid, s, api), home_kb(uid, bool(api)))
        await c.answer("Upisano sa API-ja: " + ", ".join(changed) if changed else "API nije odgovorio", show_alert=bool(changed))
        return

    if action == "open":
        await safe_edit(c, *list_text(uid, track.OPEN))
    elif action == "done":
        await safe_edit(c, *list_text(uid, track.SETTLED))
    elif action == "stats":
        days = int(parts[2])
        await safe_edit(c, stats_text(uid, days), stats_kb(days))
    elif action == "hist":
        b = InlineKeyboardBuilder()
        b.button(text="📒 Balans", callback_data="tr:home")
        await safe_edit(c, history_text(uid), b.as_markup())
    elif action == "manual":
        awaiting[uid] = "tr:manual"
        await c.message.answer(MANUAL_HELP)
    elif action == "pick":  # which bookie (🔁: which one the money leaves)
        what = parts[2]
        title = {"dep": "➕ Uplata – na koju kladionicu?", "wd": "➖ Isplata – sa koje kladionice?",
                 "mv": "🔁 Prebaci – <b>sa</b> koje kladionice?", "fix": "✏️ Ispravi balans – koje kladionice?"}[what]
        names = bookie_names(uid, s)
        if not names:
            await c.answer("Uključi bar jednu kladionicu u 🏦 Kladionice.", show_alert=True)
            return
        await safe_edit(c, title, pick_kb(names, what, book.balances(uid)))
    elif action == "b":  # a bookie picked
        what, name = parts[2], parts[3]
        cur, have = cur_of(name), book.balances(uid).get(name, 0.0)
        if what == "mv":
            names = bookie_names(uid, s, skip=name)
            await safe_edit(c, f"🔁 Sa <b>{name}</b> – <b>na</b> koju kladionicu?",
                            pick_kb(names, f"mv2_{name}", book.balances(uid), back="tr:pick:mv"))
        elif what.startswith("mv2_"):
            src = what[4:]
            awaiting[uid] = f"tr:mv:{src}:{name}"
            await c.message.answer(f"🔁 <b>{src} → {name}</b>: koliko si poslao? Ako je stiglo manje (naknada), "
                                   f"upiši oba: poslato pa stiglo, npr. <code>{example(cur)} "
                                   f"{float(example(cur)) * 0.98:g}</code>")
        else:
            awaiting[uid] = f"tr:{what}:{name}"
            await c.message.answer(MONEY_ASK[what].format(b=name, c=cur, ex=example(cur), have=money(have, cur)))
    elif action == "t":
        await _ticket(c, uid, int(parts[2]))
        return
    elif action == "w":  # this leg went through, the others lost
        tid, leg_id = int(parts[2]), int(parts[3])
        t = book.ticket(tid, uid)
        if t is None or t["status"] != track.OPEN:
            await c.answer("Tiket je već zatvoren.", show_alert=True)
            return
        profit = book.win(tid, leg_id)
        await _ticket(c, uid, tid, note=f"Upisano: {signed(profit, t['currency'])} {t['currency']}")
        return
    elif action == "man":
        await _ticket(c, uid, int(parts[2]), manual=True)
        return
    elif action == "cy":  # manual: next result for one leg
        tid, leg_id = int(parts[2]), int(parts[3])
        t = book.ticket(tid, uid)
        if t is None or t["status"] != track.OPEN:
            await c.answer("Tiket je već zatvoren.", show_alert=True)
            return
        leg = next((l for l in book.legs(tid) if l["id"] == leg_id), None)
        if leg is not None:
            book.set_result(tid, leg_id, RESULT_CYCLE[(RESULT_CYCLE.index(leg["result"]) + 1) % len(RESULT_CYCLE)])
        await _ticket(c, uid, tid, manual=True)
        return
    elif action == "close":
        tid = int(parts[2])
        t = book.ticket(tid, uid)
        if t is None or t["status"] != track.OPEN:
            await c.answer("Tiket je već zatvoren.", show_alert=True)
            return
        if any(not l["result"] for l in book.legs(tid)):
            await c.answer("Prvo za svaku nogu izaberi ishod (klik menja ⏳ → ✅ → ❌ → ↩️ → 🌗 → 🌘).", show_alert=True)
            return
        profit = book.settle(tid)
        await _ticket(c, uid, tid, note=f"Upisano: {signed(profit, t['currency'])} {t['currency']}")
        return
    elif action == "undo":
        tid = int(parts[2])
        if book.ticket(tid, uid) is not None:
            book.reopen(tid)
        await _ticket(c, uid, tid, note="Rezultat poništen – dobitak je skinut sa balansa")
        return
    elif action == "del":
        tid = int(parts[2])
        b = InlineKeyboardBuilder()
        b.button(text="🗑 Da, obriši (ulozi se vraćaju na balans)", callback_data=f"tr:delok:{tid}")
        b.button(text="⬅️ Ne", callback_data=f"tr:t:{tid}")
        b.adjust(1)
        await safe_edit(c, markup=b.as_markup())
    elif action == "delok":
        tid = int(parts[2])
        if book.ticket(tid, uid) is not None:
            book.delete(tid)
        await safe_edit(c, *list_text(uid, track.OPEN))
        await c.answer("Tiket obrisan")
        return
    elif action == "dsave":
        d = drafts.pop(uid, None)
        if d is None:
            await c.answer("Ovaj tiket je već sačuvan ili otkazan.", show_alert=True)
            return
        awaiting.pop(uid, None)
        tid = book.add_ticket(uid, d.name, d.legs, d.currency, start=d.start, sport=d.sport, market=d.market,
                              arb_key=d.arb_key, planned=d.planned)
        await _ticket(c, uid, tid, note="✅ Sačuvano – ulozi su skinuti sa balansa. Posle meča te pitam ko je prošao.")
        return
    elif action == "dleg":
        d = drafts.get(uid)
        i = int(parts[2])
        if d is None or i >= len(d.legs):
            await c.answer("Ovaj tiket je već sačuvan ili otkazan.", show_alert=True)
            return
        awaiting[uid] = f"tr:dleg:{i}"
        l = d.legs[i]
        await c.message.answer(f"✏️ <b>{l['bookie']}</b>: {l['label']} – upiši ulog i kvotu kako si stvarno uplatio "
                               f"(npr. <code>{money(l['stake'], d.currency).replace('.', '')} {fmt_odd(l['odd'])}</code>), "
                               "samo ulog, <code>@kvota</code>, ili <code>0</code> ako tu nisi uplatio.")
    elif action == "dcancel":
        drafts.pop(uid, None)
        awaiting.pop(uid, None)
        await safe_edit(c, "❌ Tiket nije sačuvan.")
    await c.answer()


async def _ticket(c: CallbackQuery, uid: int, tid: int, manual: bool = False, note: str = "") -> None:
    t = book.ticket(tid, uid)
    if t is None:
        await c.answer("Tog tiketa više nema.", show_alert=True)
        return
    legs = book.legs(tid)
    text = ticket_text(t, legs)
    if manual and t["status"] == track.OPEN:
        text += "\n\nKlikni nogu da promeniš ishod (⏳ → ✅ → ❌ → ↩️ → 🌗 → 🌘), pa 💾 Zatvori tiket."
    if note:
        text = f"{note}\n\n{text}"
    if t["status"] == track.SETTLED:
        bal = book.balances(uid)
        cur = t["currency"]
        text += "\n" + " · ".join(f"{b}: {money(bal.get(b, 0.0), cur)}" for b in dict.fromkeys(l["bookie"] for l in legs))
    await safe_edit(c, text, ticket_kb(t, legs, manual))
    await c.answer()


# ---------------------------------------------------------------- "⏰ ko je prošao?" after the match

async def remind_loop(bot: Bot) -> None:
    while True:
        try:
            await remind(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("📒 reminders failed")
        await asyncio.sleep(REMIND_EVERY)


async def remind(bot: Bot) -> int:
    sent = 0
    for t in await asyncio.to_thread(book.due):
        book.mark_reminded(t["id"])
        if not allowed(t["uid"]):
            continue
        legs = book.legs(t["id"])
        try:
            await bot.send_message(t["uid"], ticket_text(t, legs, head="⏰ <b>Meč je verovatno gotov – ko je prošao?</b>"),
                                   reply_markup=ticket_kb(t, legs))
            sent += 1
        except Exception:
            log.exception("📒 reminder for ticket %s failed", t["id"])
    return sent
