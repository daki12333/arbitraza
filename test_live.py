"""Offline tests for real betting (arb.live): no network, fake exchanges.

    python test_live.py        (or: python -m pytest test_live.py)

Covers the SX Bet order (odds ladder, EIP-712 signature, answers), the order of the legs
and every way a run can end (both in, SX refused, SX partial, Polymarket missed -> cover /
exposed, SX unknown), the geoblock stop, the dry run, settling, and the 📈 estimate."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from arb.arbitrage import find_arbs
from arb.live import Fill, book as bk, stats
from arb.live.engine import PM, SX, AutoTrader, auto_ok, is_pair
from arb.live.sxbet import ORDER_TYPES, SXExecutor, address_of, max_prob, odd_of, parse_order, sign_order
from arb.models import Event
from arb.scrapers.sxbet import SXBetScraper, _odd
from arb.tg.storage import UserSettings

KEY = "0x" + "11" * 32


# ---------------------------------------------------------------- SX Bet order

def test_ladder_roundtrip():
    """Every ladder price -> scraper odd (rounded to 0.001) -> back to exactly that price."""
    for units in range(125, 100_000, 125):
        po = units * 10 ** 15
        o = _odd({"percentageOdds": str(po)})
        if o is not None:
            assert max_prob(o) == po, (units, o, max_prob(o))


def test_ladder_never_worse():
    """Between ladder steps the price goes down (better odds for us), never up past the slack."""
    for odd in [1.01 + i / 1000 for i in range(0, 9000, 7)]:
        po = max_prob(odd)
        assert odd_of(po) >= odd - 0.0006, (odd, odd_of(po))  # at most the scraper's own rounding
    assert max_prob(1.0) is None


def test_signature_recovers():
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    domain = {"name": "SX Bet", "version": "1", "chainId": 4162, "verifyingContract": "0x" + "22" * 20}
    order = {"marketHash": "0x" + "ab" * 32, "baseToken": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
             "totalBetSize": 2_000_000, "percentageOdds": 52_500_000_000_000_000_000, "salt": 7,
             "expiry": 1_800_000_000, "maker": address_of(KEY), "isMakerBettingOutcomeOne": True}
    sig = sign_order(order, domain, KEY)
    assert len(sig) == 132
    signable = encode_typed_data(domain_data=domain, message_types=ORDER_TYPES, message_data=order)
    assert Account.recover_message(signable, signature=sig) == address_of(KEY)


def test_build_body():
    ex = SXExecutor(client=SimpleNamespace())
    ex._key, ex.address = KEY, address_of(KEY)
    ex._meta = {"domain": {"name": "SX Bet", "version": "1", "chainId": 4162, "verifyingContract": "0x" + "22" * 20},
                "activeAsset": {"baseToken": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"}, "oddsLadderStepSize": 125}
    body = ex.build({"market": "0x" + "cd" * 32, "one": False}, 3.456, 1.95)
    assert body["totalBetSize"] == "3450000"  # cents only
    assert body["timeInForce"] == "FOK" and body["isMakerBettingOutcomeOne"] is False
    assert body["salt"].startswith("0x") and len(body["salt"]) == 66
    assert odd_of(int(body["percentageOdds"])) >= 1.95 - 0.002
    assert body["maker"] == address_of(KEY)


def test_parse_answers():
    ok = {"data": {"orders": [{"orderId": "0xa", "status": "ACCEPTED",
                               "outcome": {"state": "FULLY_FILLED", "fillAmount": "5000000", "remainingAmount": "0"}}]}}
    f = parse_order(ok, 5.0, 2.0)
    assert f.ok and f.stake == 5.0 and f.payout == 10.0 and f.order_id == "0xa"
    part = {"data": {"orders": [{"orderId": "0xb", "outcome": {"state": "PARTIAL_FILL_DONE", "fillAmount": "2000000"}}]}}
    f = parse_order(part, 5.0, 2.0)
    assert f.ok and f.stake == 2.0
    no = {"data": {"orders": [{"orderId": "0xc", "outcome": {"state": "CANCELLED", "cancelReason": "NO_LIQUIDITY"}}]}}
    f = parse_order(no, 5.0, 2.0)
    assert not f.ok and not f.unknown and "ponude" in f.error
    failed = {"data": {"orders": [{"status": "FAILED", "reason": "INSUFFICIENT_BALANCE"}]}}
    f = parse_order(failed, 5.0, 2.0)
    assert not f.ok and not f.unknown and f.error == "INSUFFICIENT_BALANCE"
    late = {"data": {"orders": [{"orderId": "0xd", "outcome": {"state": "TIMEOUT"}}]}}
    f = parse_order(late, 5.0, 2.0)
    assert not f.ok and f.unknown


# ---------------------------------------------------------------- scraper refs

def test_sx_scraper_refs():
    sc = SXBetScraper()
    start = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
    base = {"sportId": 5, "status": "ACTIVE", "gameTime": start, "teamOneName": "Alpha", "teamTwoName": "Beta",
            "sportXeventId": "E1", "leagueLabel": "L", "sportLabel": "Soccer"}
    markets = [
        {**base, "marketHash": "0xh1", "type": 1, "outcomeOneName": "Alpha", "outcomeTwoName": "Not Alpha"},
        {**base, "marketHash": "0xh2", "type": 3, "line": 1.5, "outcomeOneName": "Beta +1.5",
         "outcomeTwoName": "Alpha -1.5"},
    ]
    lvl = lambda p: {"percentageOdds": str(int(p * 1e20)), "size": "50000000"}
    best = {"0xh1": {"outcomeOne": lvl(0.45), "outcomeTwo": lvl(0.56)},
            "0xh2": {"outcomeOne": lvl(0.70), "outcomeTwo": lvl(0.31)}}
    ev = sc._build(markets, best)[0]
    assert ev.bet_ref[("1X2", "1")] == {"market": "0xh1", "one": True, "event": "E1"}
    assert ev.bet_ref[("DC", "X2")]["one"] is False
    # outcome one is the away side here: home's -1.5 is outcome two
    assert ev.bet_ref[("AH_-1.5", "1")] == {"market": "0xh2", "one": False, "event": "E1"}
    assert ev.bet_ref[("AH_-1.5", "2")]["one"] is True
    asyncio.run(sc.close())


def test_matcher_flips_refs():
    from arb.matcher import _swap_sides

    ev = _event(SX, {"12": {"1": 2.1, "2": 1.9}}, {("12", "1"): {"one": True}, ("12", "2"): {"one": False}})
    _swap_sides(ev)
    assert ev.bet_ref[("12", "1")] == {"one": False} and ev.bet_ref[("12", "2")] == {"one": True}


# ---------------------------------------------------------------- engine

def _event(bookie, markets, refs, start=None):
    return Event(bookie=bookie, event_id=f"{bookie}-1", sport="tennis", home="Alpha", away="Beta",
                 start=start or datetime.now(timezone.utc) + timedelta(hours=2), group="g1", markets=markets,
                 bet_ref=refs)


def _arb(sx_odd=2.10, pm_odd=2.05, start=None):
    sx = _event(SX, {"12": {"1": sx_odd, "2": 1.70}}, {("12", "1"): {"market": "0xm", "one": True},
                                                       ("12", "2"): {"market": "0xm", "one": False}}, start)
    pm = _event(PM, {"12": {"1": 1.80, "2": pm_odd}}, {
        ("12", "1"): {"token": "t1", "condition": "c", "rate": 0.0, "tick": "0.01", "neg_risk": False},
        ("12", "2"): {"token": "t2", "condition": "c", "rate": 0.0, "tick": "0.01", "neg_risk": False}}, start)
    arbs = find_arbs([[sx, pm]])
    assert arbs and is_pair(arbs[0]), arbs
    a = arbs[0]
    a.checked_at = 1.0
    return a


class FakeService:
    def __init__(self, arbs):
        self.arbs = arbs

    def arbs_for(self, s):
        return self.arbs

    async def verify(self, arbs, s):
        return arbs

    def lookup(self, key, s):
        return self.arbs[0] if self.arbs else None

    def covers(self, s):
        return True


class FakeSX:
    connected = True
    address = "0x" + "11" * 20
    error = ""

    def __init__(self, fill=None, bal=50.0):
        self.fill, self.bal, self.calls = fill, bal, []
        self.client = SimpleNamespace(aclose=_noop)

    def has_key(self):
        return True

    async def balance(self, force=False):
        return self.bal

    async def buy(self, ref, stake, min_odd, dry=False):
        self.calls.append((ref, stake, min_odd, dry))
        if self.fill:
            return self.fill(stake, min_odd)
        return Fill(True, "sx1", stake, stake * min_odd)


class FakePM:
    connected = True
    error = ""

    def __init__(self, fills=None, blocked=False, bal=50.0):
        self.fills, self.blocked, self.bal, self.calls = list(fills or []), blocked, bal, []

    def has_key(self):
        return True

    async def balance(self, force=False):
        return self.bal

    async def geoblock(self):
        return self.blocked, "RS"

    async def buy(self, token, amount, worst, tick, neg_risk, rate, dry=False):
        self.calls.append((token, amount, worst, dry))
        if self.fills:
            return self.fills.pop(0)
        return Fill(True, "pm1", amount, amount / worst, worst)


async def _noop():
    return None


def _trader(arbs, sx=None, pm=None):
    tmp = Path(tempfile.mkdtemp()) / "live.db"
    s = UserSettings(mode="crypto", auto=True, auto_confirm=False, auto_stake=10, auto_daily=100, auto_open=100,
                     auto_min=0.5, auto_hours=0)
    store = SimpleNamespace(users={1: s}, get=lambda uid: s)
    sent = []

    async def send(uid, text, spec=None):
        sent.append((uid, text, spec))

    t = AutoTrader(FakeService(arbs), store, send, book=bk.Book(tmp), poly=pm or FakePM(), sx=sx or FakeSX())
    return t, s, sent


def _run(coro):
    return asyncio.run(coro)


def test_both_legs_in():
    a = _arb()
    t, s, _ = _trader([a])
    text, spec = _run(t.execute(1, s, a))
    r = t.book.run(spec[1])
    assert r["status"] == bk.OPEN, text
    assert [b["bookie"] for b in r["bets"]] == [SX, PM]  # SX first
    assert all(b["status"] == bk.PLACED for b in r["bets"])
    assert r["planned_profit"] > 0
    assert t.sx.calls[0][2] == 2.10  # SX at the planned odd, no worse


def test_sx_refused_nothing_placed():
    a = _arb()
    sx = FakeSX(fill=lambda st, o: Fill(False, error="nema dovoljno ponude po toj kvoti"))
    t, s, _ = _trader([a], sx=sx)
    text, _ = _run(t.execute(1, s, a, manual=True))
    assert "nije primio" in text
    assert not t.poly.calls  # Polymarket never touched
    assert t.book.runs(1)[0]["status"] == bk.FAILED


def test_sx_partial_resizes_pm():
    a = _arb()
    sx = FakeSX(fill=lambda st, o: Fill(True, "sx", round(st / 2, 2), round(st / 2, 2) * o))
    t, s, _ = _trader([a], sx=sx)
    _run(t.execute(1, s, a))
    sx_stake = sx.calls[0][1]
    pm_amount = t.poly.calls[0][1]
    planned_pm = 10 - sx_stake  # rounded plan is ~10 $ total
    assert abs(pm_amount - planned_pm / 2) < 0.6, (sx_stake, pm_amount)
    assert t.book.runs(1)[0]["status"] == bk.OPEN


def test_pm_miss_then_cover():
    a = _arb()
    pm = FakePM(fills=[Fill(False, error="FOK not filled")] * 3)  # 3 tries at break-even fail, cover works
    t, s, _ = _trader([a], pm=pm)
    _run(t.execute(1, s, a))
    r = t.book.runs(1)[0]
    assert r["status"] == bk.OPEN and "gubitak" in r["note"]
    assert len(pm.calls) == 4
    assert pm.calls[3][2] >= pm.calls[0][2]  # the cover accepts a worse (higher) price


def test_pm_miss_exposed_alert():
    a = _arb()
    pm = FakePM(fills=[Fill(False, error="FOK not filled")] * 4)
    t, s, _ = _trader([a], pm=pm)
    text, spec = _run(t.execute(1, s, a))
    assert spec[0] == "exposed" and "NIJE" in text
    assert t.book.runs(1)[0]["status"] == bk.EXPOSED
    pm.fills = []  # 🛟 now works
    text, spec = _run(t.cover(1, spec[1]))
    assert t.book.run(spec[1])["status"] == bk.OPEN, text


def test_sx_unknown_asks_user():
    a = _arb()
    sx = FakeSX(fill=lambda st, o: Fill(False, "0xq", st, unknown=True, error="SX Bet nije potvrdio ishod (timeout)"))
    t, s, _ = _trader([a], sx=sx)
    text, spec = _run(t.execute(1, s, a))
    assert spec[0] == "unconfirmed" and not t.poly.calls
    assert t.not_placed(1, spec[1]).startswith("✅")
    assert t.book.run(spec[1])["status"] == bk.FAILED


def test_geoblock_stops_everything():
    a = _arb()
    t, s, _ = _trader([a], pm=FakePM(blocked=True))
    text, _ = _run(t.execute(1, s, a, manual=True))
    assert "ne dozvoljava" in text and not t.sx.calls and not t.poly.calls


def test_dry_run_sends_nothing():
    a = _arb()
    t, s, _ = _trader([a])
    s.auto = False
    text, spec = _run(t.execute(1, s, a, dry=True))
    assert spec is None and "Proba" in text
    assert t.sx.calls[0][3] is True and t.poly.calls[0][3] is True
    assert t.book.exposure(1) == 0 and t.book.staked_since(1, 0) == 0


def test_limits_and_money():
    a = _arb()
    t, s, _ = _trader([a], sx=FakeSX(bal=0.5))
    text, _ = _run(t.execute(1, s, a, manual=True))
    assert "Nema dovoljno novca" in text and not t.sx.calls
    t, s, _ = _trader([a])
    s.auto_daily = 1
    text, _ = _run(t.execute(1, s, a, manual=True))
    assert "dnevni limit" in text


def test_rules():
    s = UserSettings(mode="crypto", auto_min=1.0, auto_hours=3)
    assert auto_ok(_arb(), s) is None
    assert auto_ok(_arb(start=datetime.now(timezone.utc) + timedelta(hours=5)), s) == "meč počinje kasnije od pravila"
    assert auto_ok(_arb(start=datetime.now(timezone.utc) + timedelta(seconds=30)), s) == "meč uskoro počinje"
    assert auto_ok(_arb(sx_odd=2.02, pm_odd=2.0), s) == "profit ispod pravila"


def test_settle_from_polymarket():
    import arb.live.engine as eng

    a = _arb(start=datetime.now(timezone.utc) - timedelta(hours=3))
    t, s, sent = _trader([a])
    # place a run by hand in the book (kickoff 3 h ago), then let Polymarket say its side lost
    run = t.book.new_run(1, "k", "Alpha – Beta", "Pobednik", "tennis", a.event.start.isoformat())
    t.book.add_bet(run, 1, SX, "12", "1", "Alpha", 4.76, 2.1, "sx", bk.PLACED, {})
    t.book.add_bet(run, 1, PM, "12", "2", "Beta", 5.0, 2.05, "pm", bk.PLACED, {"token": "t2", "condition": "c"})
    t.book.finish_run(run, bk.OPEN)

    async def lost(cond, tok):
        return False

    orig, eng.resolution = eng.resolution, lost
    try:
        _run(t.settle(1))
    finally:
        eng.resolution = orig
    r = t.book.run(run)
    assert r["status"] == bk.SETTLED and r["winner"] == SX
    assert abs(r["profit"] - (4.76 * 2.1 - 9.76)) < 1e-6 and sent


# ---------------------------------------------------------------- 📈 estimate

def test_estimate():
    t, s, _ = _trader([])
    a, b = _arb(), _arb(sx_odd=2.2, pm_odd=2.1)
    b.events[0].group = b.events[1].group = "g2"
    stats.record(t.book, [a, b])
    stats.record(t.book, [a])  # same arb again: still one
    e = stats.estimate(t.book, stake=10, min_pct=0.5, capital=100)
    assert e["good"] == 2 and e["profit_per_day"] > 0 and e["pct_of_capital"] is not None


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
