"""Paper trading ("test na papiru"): plays crypto arbs exactly like automatic betting
would - fresh odds, stakes for the budget, the first leg, a pause as long as a real
bet takes, the last leg re-checked - but never places a bet. Every test is stored in
data/paper.db, so we can see how often an arb would really have been caught.

What it can't see: a bookie refusing the ticket or cutting the stake at the moment
of betting - only real (small) bets show that."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field

from arb.arbitrage import Arb, Leg
from arb.config import DATA_DIR
from arb.matcher import orient
from arb.models import Event, market_label
from arb.tg.formatting import outcome_text
from arb.tg.service import arb_key, group_key

DB_FILE = DATA_DIR / "paper.db"
EXEC_DELAY = 2.0  # s - a real bet on the first bookie takes about this long before the next leg goes in
EXCHANGES = ("Polymarket", "SX Bet")  # order books: always the last leg (fills instantly, "all or nothing")
QUICK = ("1xBit",)  # sportsbooks that can re-check one game in a second: the last leg if there's no exchange
RETEST_AFTER = 20 * 60  # s - the same arb (same odds) is tested again only after this long

# result statuses
OK = "ok"  # every leg at the planned odds (or better)
OK_LESS = "ok_less"  # the last leg moved, but the bot would still take it (profit >= 0)
MISS = "miss"  # the last leg moved below break-even: one bet would be left uncovered
UNKNOWN = "unknown"  # the bookie of the last leg didn't answer in time
GONE = "gone"  # the arb was already gone when the bot looked (nothing would be bet)
NO_FIT = "no_fit"  # odds still there, but the stakes don't work for this budget


@dataclass
class LegTest:
    bookie: str
    market: str
    outcome: str
    label: str  # how the pick reads in messages ("Više 2.5 (Over)", "1 · Arsenal")
    stake: float
    odd: float  # the odd the stakes were planned with (on an order book: the average for this stake)
    payout: float  # planned return if this leg wins
    order: int  # 1 = bet first
    now_odd: float | None = None  # last leg: average odd for this stake at the second check
    now_payout: float | None = None
    status: str = OK  # ok / worse / miss / unknown / placed (the legs bet before the pause)


@dataclass
class PaperResult:
    key: str
    name: str
    market: str
    sport: str
    start: str
    status: str
    total: float = 0.0
    profit: float = 0.0  # guaranteed profit (or, on a miss, the estimated result after covering)
    planned_profit: float = 0.0
    seconds: float = 0.0  # from betting the first leg to the last leg's check (what a real bet would wait)
    legs: list[LegTest] = field(default_factory=list)
    hedge: str = ""  # on a miss: where it would be covered
    at: float = field(default_factory=time.time)


def _order(rows) -> list:
    """Sportsbooks first (they may refuse / move the odd), order books last; with no order
    book, a bookie that can re-check one game quickly goes last, so the check arrives in time."""
    return sorted(rows, key=lambda r: (r[0].bookie in EXCHANGES, r[0].bookie in QUICK))


def _leg_now(leg: Leg, ev: Event | None, stake: float) -> tuple[float | None, float | None]:
    """(average odd, payout) of `stake` on this leg with the event's current odds."""
    if ev is None:
        return None, None
    odd = ev.markets.get(leg.market, {}).get(leg.outcome)
    if not odd:
        return None, None
    now = Leg(leg.outcome, odd, leg.bookie, leg.market, ev.depth.get((leg.market, leg.outcome)),
              ev.limits.get((leg.market, leg.outcome)))
    if stake > now.max_stake + 1e-9:
        return None, None  # not that much on offer any more
    pay = now.payout(stake)
    return pay / stake, pay


async def refetch(service, arb: Arb, bookies: set[str]) -> dict[str, Event | None]:
    """These bookies' odds for the arb's match right now, turned like the group.
    A bookie missing from the result didn't answer in time; None = match no longer offered."""
    wanted = {b: [e.event_id] for b in bookies if (e := arb.event_for(b))}
    fresh = await service.scanner.fetch_bookies(wanted)
    out: dict[str, Event | None] = {}
    for b, r in fresh.items():
        if r.error:
            continue
        ev = next((x for x in r.events if x.event_id == wanted[b][0]), None)
        if ev is not None:
            ev.group = arb.event.group
            orient(arb.event, [ev])
        out[b] = ev
    return out


async def run_test(service, arb: Arb, s, currency: str) -> PaperResult:
    """One paper bet on this arb for user settings `s`."""
    t0 = time.perf_counter()
    ev = arb.event
    res = PaperResult(key=arb_key(arb), name=f"{ev.home} – {ev.away}",
                      market=market_label(arb.market, ev.sport, ev.home, ev.away),
                      sport=ev.sport, start=ev.start.isoformat(), status=GONE)
    # 1) odds right now on every leg (same check as clicking an arb)
    fresh = await service.verify([arb], s)
    if not fresh:
        res.seconds = time.perf_counter() - t0
        return res
    a = fresh[0]
    rows = a.plan(s.budget, currency)
    if not rows:
        res.status, res.seconds = NO_FIT, time.perf_counter() - t0
        return res
    rows = _order(rows)
    res.total = sum(st for _, st, _ in rows)
    res.planned_profit = min(p for _, _, p in rows) - res.total
    res.legs = [LegTest(l.bookie, l.market, l.outcome, outcome_text(a, l.outcome), st, p / st if st else l.odd, p,
                        i + 1, status="placed")
                for i, (l, st, p) in enumerate(rows)]
    # 2) the legs before the last one are "bet" now, at the odds just checked;
    #    the last one goes in after a real bet's delay - check it again then
    *_, (last_leg, last_stake, last_payout) = rows
    lt = res.legs[-1]
    t1 = time.perf_counter()  # the first leg goes in now
    await asyncio.sleep(EXEC_DELAY)
    now = await refetch(service, a, {last_leg.bookie})
    res.seconds = time.perf_counter() - t1
    if last_leg.bookie not in now:
        lt.status, res.status = "unknown", UNKNOWN
        res.profit = res.planned_profit
        return res
    lt.now_odd, lt.now_payout = _leg_now(last_leg, now[last_leg.bookie], last_stake)
    placed = [p for _, _, p in rows[:-1]]
    if lt.now_payout is not None and lt.now_payout >= last_payout - 0.005:
        lt.status, res.status = OK, OK
    elif lt.now_payout is not None and lt.now_payout >= res.total:
        lt.status, res.status = "worse", OK_LESS  # the bot accepts down to break-even
    else:
        lt.status, res.status = "miss", MISS
    if res.status != MISS:
        res.profit = min(placed + [lt.now_payout]) - res.total
        return res
    # 3) a miss: cover the uncovered bet with the best price elsewhere in the group right now
    best = (lt.now_odd, last_leg.bookie) if lt.now_odd else None  # the moved price itself is an option
    for e in service.groups.get(group_key(a.events)) or a.events:
        if e.bookie == last_leg.bookie:
            continue
        odd = e.markets.get(last_leg.market, {}).get(last_leg.outcome)
        if odd and (best is None or odd > best[0]):
            best = (odd, e.bookie)
    placed_stakes = sum(st for _, st, _ in rows[:-1])
    if best:
        # cover with a stake that pays the same as the bets already placed
        cover = min(placed) / best[0]
        res.hedge = f"{best[1]} @ {best[0]:g}, ulog {cover:.2f} $".replace(".", ",")
        res.profit = min(placed) - placed_stakes - cover
    else:
        res.hedge = ""
        res.profit = -placed_stakes  # worst case: the uncovered side wins, every placed bet loses
    return res


# ---- storage ---------------------------------------------------------------

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.execute("""CREATE TABLE IF NOT EXISTS tests (
        at REAL, uid INTEGER, key TEXT, name TEXT, market TEXT, status TEXT,
        total REAL, profit REAL, planned_profit REAL, seconds REAL, detail TEXT)""")
    return con


def save(uid: int, r: PaperResult) -> None:
    with _db() as con:
        con.execute("INSERT INTO tests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r.at, uid, r.key, r.name, r.market, r.status, r.total, r.profit, r.planned_profit,
                     r.seconds, json.dumps({"legs": [asdict(l) for l in r.legs], "hedge": r.hedge,
                                            "start": r.start, "sport": r.sport})))


def load(uid: int, since: float) -> list[dict]:
    with _db() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM tests WHERE uid = ? AND at >= ? ORDER BY at", (uid, since)).fetchall()
    return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]
