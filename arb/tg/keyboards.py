from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from arb import secrets
from arb.arbitrage import Arb
from arb.tg.formatting import money, outcome_text
from arb.tg.storage import UserSettings

BTN_LIST = "📋 Lista arbitraža"
BTN_ARBS = "🔍 Arbitraže"
BTN_BUDGET = "💰 Ulog"
BTN_BOOKIES = "🏦 Kladionice"
BTN_STATUS = "📊 Status"
BTN_AUTO = "🤖 Bot (SX + Polymarket)"
BTN_TRACK = "📒 Tiketi i balans"

BUDGETS = {"din": [10_000, 20_000, 50_000, 100_000, 200_000, 500_000],
           "$": [25, 50, 100, 250, 500, 1_000]}


def main_menu(s: UserSettings) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_ARBS)],
        [KeyboardButton(text=f"{BTN_BUDGET}: {money(s.budget, s.currency)} {s.currency}"), KeyboardButton(text=BTN_STATUS)],
        [KeyboardButton(text=BTN_BOOKIES), KeyboardButton(text=BTN_NOTIFY), KeyboardButton(text=BTN_MIDDLES)],
    ]
    rows.append([KeyboardButton(text=BTN_TRACK)])  # 📒 tickets + money the user plays by hand (/tiketi)
    if s.mode == "crypto":  # real automatic betting SX Bet + Polymarket (/bot)
        rows[-1].append(KeyboardButton(text=BTN_AUTO))
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Upiši koliko ulažeš, npr. " + ("100" if s.currency == "$" else "50000"),
    )


LIST_HOURS = [0, 24, 6, 3]


def list_kb(keys: list[str], first: int, live: bool, page: int, pages: int,
            hours: int = 0, sort: str = "pct") -> InlineKeyboardMarkup:
    """Numbered buttons for this page (open one arb in detail), paging, refresh / live toggle."""
    b = InlineKeyboardBuilder()
    for i, key in enumerate(keys, first + 1):
        b.button(text=str(i), callback_data=f"ad:{key}")
    rows = [5] * (len(keys) // 5) + ([len(keys) % 5] if len(keys) % 5 else [])
    if pages > 1:
        b.button(text="◀️", callback_data=f"ls:page:{(page - 1) % pages}")
        b.button(text=f"{page + 1}/{pages}", callback_data="ls:refresh")
        b.button(text="▶️", callback_data=f"ls:page:{(page + 1) % pages}")
        rows.append(3)
    for h in LIST_HOURS:
        label = "Sve" if not h else f"{h}h"
        b.button(text=f"✅ {label}" if h == hours else label, callback_data=f"ls:hours:{h}")
    b.button(text="🔃 Po vremenu" if sort == "pct" else "🔃 Po profitu", callback_data="ls:sort")
    b.button(text="🔄 Osveži", callback_data="ls:refresh")
    b.button(text="⏸ Zaustavi praćenje" if live else "▶️ Prati uživo", callback_data="ls:stop" if live else "ls:live")
    b.adjust(*rows, len(LIST_HOURS), 1, 2)
    return b.as_markup()


NOTIFY_MINS = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]
NOTIFY_HOURS = [0, 24, 12, 6, 3]  # 0 = any kickoff time


BTN_NOTIFY = "🔔 Obaveštenja"
BTN_MIDDLES = "🎯 Srednjice"


def middles_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if pages > 1:
        b.button(text="◀️", callback_data=f"md:page:{(page - 1) % pages}")
        b.button(text=f"{page + 1}/{pages}", callback_data=f"md:page:{page}")
        b.button(text="▶️", callback_data=f"md:page:{(page + 1) % pages}")
    b.button(text="🔄 Osveži", callback_data=f"md:page:{page}")
    b.adjust(3, 1) if pages > 1 else b.adjust(1)
    return b.as_markup()


def notify_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Uključena" if s.notify else "⬜ Isključena", callback_data="nt:toggle")
    for p in NOTIFY_MINS:
        mark = "✅ " if p == s.notify_min else ""
        b.button(text=f"{mark}{p:g}%", callback_data=f"nt:min:{p}")
    b.button(text="✏️ Upiši svoj %", callback_data="nt:custom")
    for h in NOTIFY_HOURS:
        mark = "✅ " if h == s.notify_hours else ""
        b.button(text=f"{mark}{'⏰ Bilo kad' if h == 0 else f'{h}h'}", callback_data=f"nt:h:{h}")
    b.button(text="✏️ Upiši sate", callback_data="nt:hcustom")
    b.adjust(1, 4, 3, 1, len(NOTIFY_HOURS), 1)
    return b.as_markup()


# /bottest: the rules of the paper test (arb.paper)
BOT_HOURS = [1, 3, 6, 12, 24, 0]  # 0 = any kickoff time
BOT_MINS = [0.5, 1.0, 1.5, 2.0, 3.0]
BOT_STAKES = [10, 25, 50, 100, 250]
BOT_DELAYS = [5, 30, 60, 150, 300]  # s between the first and the last leg


def bot_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧪 Test: ✅ uključen (klik = isključi)" if s.paper else "🧪 Test: ⬜ isključen (klik = uključi)",
             callback_data="bt:toggle")
    for h in BOT_HOURS:
        mark = "✅ " if h == s.bot_hours else ""
        b.button(text=f"{mark}{'bilo kad' if h == 0 else f'⏰ {h}h'}", callback_data=f"bt:h:{h}")
    b.button(text="✏️ Upiši sate", callback_data="bt:hcustom")
    for p in BOT_MINS:
        mark = "✅ " if p == s.bot_min else ""
        b.button(text=f"{mark}📈 {p:g}%", callback_data=f"bt:min:{p}")
    b.button(text="✏️ Upiši %", callback_data="bt:mcustom")
    for a in BOT_STAKES:
        mark = "✅ " if a == s.bot_stake else ""
        b.button(text=f"{mark}💵 {a} $", callback_data=f"bt:st:{a}")
    b.button(text="✏️ Upiši max ulog", callback_data="bt:scustom")
    for d in BOT_DELAYS:
        mark = "✅ " if d == s.bot_delay else ""
        b.button(text=f"{mark}⏱ {d} s" if d < 60 else f"{mark}⏱ {d / 60:g} min".replace(".", ","),
                 callback_data=f"bt:d:{d}")
    b.button(text="💼 Novac po kladionicama" + (f" ({len(s.bot_wallets)})" if s.bot_wallets else ""),
             callback_data="bt:wallets")
    rows = [1, 3, 3, 1, 3, 2, 1, 3, 2, 1, 3, 2, 1]
    if not s.bot_wallets:  # one shared budget only while no money per bookie is set
        b.button(text=f"✏️ Zajednički budžet: {money(s.bot_bank, '$')} $", callback_data="bt:bcustom")
        rows.append(1)
    b.button(text="📊 Izveštaj (danas)", callback_data="bt:report")
    b.button(text="📊 Parovi kladionica", callback_data="bt:pairs")
    b.button(text="🔄 Kreni ispočetka", callback_data="bt:reset")
    b.adjust(*rows, 2, 1)
    return b.as_markup()


def wallets_kb(s: UserSettings) -> InlineKeyboardMarkup:
    """💼 money per bookie: tap one, then type the amount."""
    b = InlineKeyboardBuilder()
    names = s.mode_bookies
    for name in names:
        have = s.bot_wallets.get(name)
        b.button(text=f"✅ {name}: {money(have, '$')} $" if have else f"⬜ {name}", callback_data=f"bw:{name}")
    b.button(text="⬅️ Nazad na /bottest", callback_data="bt:back")
    b.adjust(*([2] * (len(names) // 2)), *([1] if len(names) % 2 else []), 1)
    return b.as_markup()


# arbs as last shown to the user, for "✍️ Odigrao sam" (arb.tg.tracker): token -> (arb key, arb)
SHOWN: dict[int, tuple[str, Arb]] = {}
_SHOWN_MAX = 300
_shown_next = [0]


def remember_shown(key: str, arb: Arb) -> int:
    """A short token for this arb exactly as the user sees it now (the odds may move later)."""
    _shown_next[0] += 1
    SHOWN[_shown_next[0]] = (key, arb)
    while len(SHOWN) > _SHOWN_MAX:
        SHOWN.pop(next(iter(SHOWN)))
    return _shown_next[0]


# Arb callbacks: "<action>:<group key>:<market>:<budget>"
def arb_kb(arb: Arb, key: str, budget: float) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # one "open the match" button per bookie involved
    seen = []
    for leg in arb.legs:
        e = arb.event_for(leg.bookie)
        if leg.bookie in seen or not e or not e.url:
            continue
        seen.append(leg.bookie)
        outs = ", ".join(outcome_text(arb, l.outcome, short=True) for l in arb.legs if l.bookie == leg.bookie)
        b.button(text=f"🔗 {leg.bookie} ({outs})", url=e.url)
    b.button(text="💰 Promeni ulog", callback_data=f"st:{key}:{budget}")
    b.button(text="🔍 Proveri kvote sad", callback_data=f"rf:{key}:{budget}")
    # 📒 the user played it by hand: the bot keeps the ticket and the money (arb.tg.tracker)
    b.button(text="✍️ Odigrao sam – prati tiket", callback_data=f"tk:{remember_shown(key, arb)}:{budget}")
    b.button(text="❌ Sakrij", callback_data="del")
    b.adjust(*([1] * len(seen)), 2, 1, 1)
    return b.as_markup()


def arb_budget_kb(key: str, budget: float, currency: str = "din") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in BUDGETS[currency]:
        mark = "✅ " if a == budget else ""
        b.button(text=f"{mark}{money(a, currency)}", callback_data=f"rf:{key}:{a}")
    b.button(text="✏️ Upiši svoj iznos", callback_data=f"cu:{key}:{budget}")
    b.button(text="⬅️ Nazad", callback_data=f"rf:{key}:{budget}")
    b.adjust(3, 3, 1, 1)
    return b.as_markup()


def budget_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in BUDGETS[s.currency]:
        mark = "✅ " if a == s.budget else ""
        b.button(text=f"{mark}{money(a, s.currency)}", callback_data=f"set:budget:{a}")
    b.button(text="✏️ Upiši svoj iznos", callback_data="set:custom:")
    b.adjust(3, 3, 1)
    return b.as_markup()


def bookies_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    names = s.mode_bookies
    for name in names:
        mark = "✅" if name in s.bookies else "⬜"
        b.button(text=f"{mark} {name}", callback_data=f"set:bk:{name}")
    extra = 1
    if s.mode == "crypto":
        has_key = bool(secrets.get("sxbet_api_key"))
        b.button(text="🔑 SX Bet ključ ✅ (promeni)" if has_key else "🔑 Unesi SX Bet API ključ", callback_data="set:sxkey:")
        b.button(text="🤖 Automatsko klađenje SX Bet + Polymarket (/bot)", callback_data="au:back")
        extra = 3
    switch = "🇷🇸 Prebaci na srpske kladionice" if s.mode == "crypto" else "🪙 Prebaci na kripto"
    b.button(text=switch, callback_data="set:mode:")
    b.adjust(*([2] * (len(names) // 2)), *([1] if len(names) % 2 else []), *([1] * extra))
    return b.as_markup()
