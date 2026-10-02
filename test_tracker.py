"""Offline tests for 📒 Tiketi i balans (arb.track + arb.tg.tracker): no network, no Telegram.

    python test_tracker.py        (or: python -m pytest test_tracker.py)

Covers the money book (uplata, isplata, prebaci with fees, ispravi), tickets (stakes off the
balance, a leg won, void / half results, undo, delete), stats, the typed ticket and amounts,
and the Telegram flow: ✍️ Odigrao sam under an arb -> edit a leg -> ✅ Sačuvaj -> ⏰ reminder
after the match -> ✅ Prošao."""
from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from arb import track
from arb.arbitrage import find_arbs
from arb.models import Event
from arb.tg import keyboards as kb, tracker as tr
from arb.tg.handlers import awaiting
from arb.tg.storage import UserSettings

UID = 7


def _book() -> track.Tracker:
    return track.Tracker(Path(tempfile.mkdtemp()) / "t.db")


def _legs(a_stake=50.0, b_stake=50.0):
    return [{"bookie": "Mozzart", "outcome": "1", "label": "1", "odd": 2.1, "stake": a_stake},
            {"bookie": "Meridian", "outcome": "2", "label": "2", "odd": 2.1, "stake": b_stake}]


# ---------------------------------------------------------------- the book

def test_money_moves():
    b = _book()
    b.deposit(UID, "Mozzart", 10_000)
    b.deposit(UID, "Meridian", 5_000)
    b.withdraw(UID, "Mozzart", 1_000)
    b.transfer(UID, "Mozzart", "Meridian", 2_000, 1_950)  # 50 fees
    assert b.balances(UID) == {"Mozzart": 7_000, "Meridian": 6_950}
    assert b.fix(UID, "Meridian", 7_000) == 50
    assert b.fix(UID, "Meridian", 7_000) == 0
    assert b.balances(UID)["Meridian"] == 7_000
    assert b.balances(UID + 1) == {}  # other users see nothing


def test_ticket_won():
    b = _book()
    b.deposit(UID, "Mozzart", 100)
    b.deposit(UID, "Meridian", 100)
    tid = b.add_ticket(UID, "A – B", _legs(), "din", start=time.time() + 3600, planned=5)
    assert b.balances(UID) == {"Mozzart": 50, "Meridian": 50}
    assert b.in_play(UID) == {"Mozzart": 100 - 50, "Meridian": 50}
    leg = b.legs(tid)[0]
    profit = b.win(tid, leg["id"])
    assert abs(profit - (50 * 2.1 - 100)) < 1e-9
    assert abs(b.balances(UID)["Mozzart"] - (50 + 105)) < 1e-9 and b.balances(UID)["Meridian"] == 50
    assert b.in_play(UID) == {}
    try:
        b.settle(tid)
        raise AssertionError("settled twice")
    except ValueError:
        pass


def test_ticket_void_half_undo_delete():
    b = _book()
    tid = b.add_ticket(UID, "A – B", _legs(), "$")
    l1, l2 = b.legs(tid)
    b.set_result(tid, l1["id"], track.VOID)
    try:
        b.settle(tid)
        raise AssertionError("settled with a leg missing")
    except ValueError:
        pass
    b.set_result(tid, l2["id"], track.HALF_WON)
    profit = b.settle(tid)
    assert abs(profit - (50 + 25 * 2.1 + 25 - 100)) < 1e-9
    b.reopen(tid)  # wrong result: payouts off again
    assert b.ticket(tid)["status"] == track.OPEN and b.balances(UID) == {"Mozzart": -50, "Meridian": -50}
    b.delete(tid)  # entered by mistake: as if it never was
    assert b.balances(UID) == {} and b.ticket(tid) is None
    assert track.leg_payout(10, 2.0, track.HALF_LOST) == 5


def test_due_and_stats():
    b = _book()
    old = b.add_ticket(UID, "Old", _legs(), "din", start=time.time() - track.FINISH_AFTER - 60, planned=5)
    b.add_ticket(UID, "Later", _legs(), "din", start=time.time() + 3600)
    assert [t["id"] for t in b.due()] == [old]
    b.mark_reminded(old)
    assert b.due() == []
    b.win(old, b.legs(old)[1]["id"])
    st = b.stats(UID)["din"]
    assert st["tickets"] == 1 and st["open"] == 1 and st["staked"] == 100
    assert abs(st["profit"] - 5) < 1e-9 and st["plus"] == 1
    assert st["bookies"]["Meridian"]["net"] > 0 > st["bookies"]["Mozzart"]["net"]
    assert st["pairs"]["Meridian + Mozzart"]["n"] == 1


# ---------------------------------------------------------------- typed values

def test_parse_money_and_odd():
    assert tr.parse_money("5000") == 5000
    assert tr.parse_money("5.000") == 5000
    assert tr.parse_money("12.345,50") == 12345.5
    assert tr.parse_money("5 000 din") == 5000
    assert tr.parse_money("1,5k") == 1500
    assert tr.parse_money("10.5") == 10.5
    assert tr.parse_money("10,25 $") == 10.25
    assert tr.parse_money("abc") is None
    assert tr.parse_odd("2,05") == 2.05 and tr.parse_odd("@1.9") == 1.9 and tr.parse_odd("1") is None


def test_parse_manual():
    d, err = tr.parse_manual("Real – Barcelona 20:45\nMozzart 1 2.10 5000\nmeridian više 2.5 1,95 5.200")
    assert d is not None, err
    assert d.name == "Real – Barcelona" and d.currency == "din" and len(d.legs) == 2
    assert d.legs[1] == {"bookie": "Meridian", "outcome": "više 2.5", "label": "više 2.5", "odd": 1.95, "stake": 5200}
    assert datetime.fromtimestamp(d.start, tr.TZ).strftime("%H:%M") == "20:45"
    _, err = tr.parse_manual("X\nNepoznata 1 2.0 100")
    assert "kladionicu" in err
    _, err = tr.parse_manual("X\nMozzart 1 2.0 100\n1xBit 2 2.1 10")
    assert "dinare" in err  # din + $ in one ticket
    name, start = tr.parse_start("A – B 03.10. 18:30")
    assert name == "A – B" and datetime.fromtimestamp(start, tr.TZ).strftime("%d.%m %H:%M") == "03.10 18:30"


# ---------------------------------------------------------------- Telegram flow

def _msg(text=""):
    sent = MagicMock()
    sent.chat.id, sent.message_id = UID, 100
    m = MagicMock()
    m.text = text
    m.from_user.id = UID
    m.answer = AsyncMock(return_value=sent)
    return m


def _cb(data):
    c = MagicMock()
    c.data = data
    c.from_user.id = UID
    c.answer = AsyncMock()
    c.message.edit_text = AsyncMock()
    c.message.edit_reply_markup = AsyncMock()
    sent = MagicMock()
    sent.chat.id, sent.message_id = UID, 101
    c.message.answer = AsyncMock(return_value=sent)
    return c


class Store:
    def __init__(self):
        self.s = UserSettings()

    def get(self, uid):
        return self.s

    def save(self):
        pass


def _arb():
    start = datetime.now(timezone.utc) - timedelta(hours=4)  # already over: the reminder fires

    def ev(bookie, odds):
        return Event(bookie=bookie, event_id=f"{bookie}-1", sport="tennis", home="Alpha", away="Beta",
                     start=start, group="g1", markets={"12": odds})
    arbs = find_arbs([[ev("Mozzart", {"1": 2.15, "2": 1.6}), ev("Meridian", {"1": 1.7, "2": 2.1})]])
    assert arbs
    return arbs[0]


def _texts(mock) -> str:
    return "\n".join(str(c.args[-1]) if c.args else str(c.kwargs.get("text", "")) for c in mock.call_args_list)


def test_flow_played_edit_save_remind_win():
    tr.book = _book()
    tr.drafts.clear()
    awaiting.clear()
    store, bot = Store(), MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()

    async def run():
        # ➕ uplata on both
        for name, amount in (("Mozzart", "20.000"), ("Meridian", "20000")):
            awaiting[UID] = f"tr:dep:{name}"
            await tr.typed(_msg(amount), bot, store)
        assert tr.book.balances(UID) == {"Mozzart": 20_000, "Meridian": 20_000}

        # ✍️ Odigrao sam under the arb message
        arb = _arb()
        markup = kb.arb_kb(arb, "g1:12", 10_000)
        data = next(b.callback_data for row in markup.inline_keyboard for b in row
                    if (b.callback_data or "").startswith("tk:"))
        assert len(data.encode()) <= 64
        c = _cb(data)
        await tr.cb_played(c, store)
        d = tr.drafts[UID]
        assert {l["bookie"] for l in d.legs} == {"Mozzart", "Meridian"} and d.planned > 0
        assert "Novi tiket" in _texts(c.message.answer)

        # the Mozzart leg went in at another odd: ✏️ then "5000 2.10"
        i = next(i for i, l in enumerate(d.legs) if l["bookie"] == "Mozzart")
        await tr.cb(_cb(f"tr:dleg:{i}"), store)
        assert awaiting[UID] == f"tr:dleg:{i}"
        await tr.typed(_msg("5000 2.10"), bot, store)
        assert d.legs[i]["stake"] == 5000 and d.legs[i]["odd"] == 2.10
        bot.edit_message_text.assert_awaited()

        # ✅ Sačuvaj
        await tr.cb(_cb("tr:dsave"), store)
        assert UID not in tr.drafts
        (t,) = tr.book.tickets(UID)
        other = sum(l["stake"] for l in tr.book.legs(t["id"]) if l["bookie"] == "Meridian")
        assert tr.book.balances(UID) == {"Mozzart": 15_000, "Meridian": 20_000 - other}

        # the match started 4 h ago: ⏰ asks once
        assert await tr.remind(bot) == 1 and await tr.remind(bot) == 0
        assert "ko je prošao" in _texts(bot.send_message).lower()

        # ✅ Prošao: Mozzart
        leg = next(l for l in tr.book.legs(t["id"]) if l["bookie"] == "Mozzart")
        c = _cb(f"tr:w:{t['id']}:{leg['id']}")
        await tr.cb(c, store)
        assert tr.book.ticket(t["id"])["status"] == track.SETTLED
        assert tr.book.balances(UID)["Mozzart"] == 15_000 + 5000 * 2.10
        assert "Završen" in _texts(c.message.edit_text)

        # 📒 home, 📊 stats, 🧾 history, 🔁 prebaci render
        text = tr.home_text(UID, store.s)
        assert "Mozzart" in text and "Poslednjih 30 dana" in text
        assert "Promet" in tr.stats_text(UID, 30)
        assert "dobitak" in tr.history_text(UID)
        awaiting[UID] = "tr:mv:Mozzart:Meridian"
        await tr.typed(_msg("1000 990"), bot, store)
        assert tr.book.balances(UID)["Mozzart"] == 15_000 + 5000 * 2.10 - 1000

        # a menu button while a number is awaited: the question is dropped, the button works
        awaiting[UID] = "tr:dep:Mozzart"
        assert not tr._awaiting_tr(_msg(kb.BTN_LIST)) and UID not in awaiting

    asyncio.run(run())


def test_manual_ticket_flow():
    tr.book = _book()
    tr.drafts.clear()
    awaiting.clear()
    store, bot = Store(), MagicMock()

    async def run():
        await tr.cb(_cb("tr:manual"), store)
        await tr.typed(_msg("Alpha – Beta\nPolymarket Yes 2.0 10\nStake No 2.1 10"), bot, store)
        assert tr.drafts[UID].currency == "$"
        await tr.cb(_cb("tr:dsave"), store)
        (t,) = tr.book.tickets(UID)
        l1, l2 = tr.book.legs(t["id"])
        # ✏️ Drugo: Polymarket void, Stake lost, then 💾
        for _ in range(3):  # ⏳ -> ✅ -> ❌ -> ↩️
            await tr.cb(_cb(f"tr:cy:{t['id']}:{l1['id']}"), store)
        for _ in range(2):
            await tr.cb(_cb(f"tr:cy:{t['id']}:{l2['id']}"), store)
        await tr.cb(_cb(f"tr:close:{t['id']}"), store)
        assert tr.book.ticket(t["id"])["profit"] == -10
        assert tr.book.balances(UID) == {"Polymarket": 0, "Stake": -10}

    asyncio.run(run())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except Exception as e:  # noqa: BLE001 - report every test
            failed += 1
            print(f"❌ {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} prošlo")
    raise SystemExit(1 if failed else 0)
