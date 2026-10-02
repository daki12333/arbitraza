"""Paper trading ("test na papiru"): plays crypto arbs exactly like automatic betting
would - fresh odds, stakes for the budget, the first leg, a pause as long as a real
bet takes, the last leg re-checked - but never places a bet. Every test is stored in
data/paper.db, so we can see how often an arb would really have been caught.

What it can't see: a bookie refusing the ticket or cutting the stake at the moment
of betting - only real (small) bets show that."""
from __future__ import annotations

import asyncio
import json
import math
import random
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from arb.arbitrage import Arb, Leg
from arb.config import DATA_DIR
from arb.matcher import orient
from arb.models import Event, market_label
from arb.tg.formatting import outcome_text
from arb.tg.service import arb_key, group_key

DB_FILE = DATA_DIR / "paper.db"
EXEC_DELAY = 5.0  # s - from the first leg to the last when the money already sits on both bookies
# (/bot can set it longer, e.g. 150 s when the money has to be sent over Solana first)
EXCHANGES = ("Polymarket", "SX Bet")  # order books: always the last leg (fills instantly, "all or nothing")
QUICK = ("1xBit",)  # sportsbooks that can re-check one game in a second: the last leg if there's no exchange
RETEST_AFTER = 20 * 60  # s - the same arb (same odds) is tested again only after this long
MIN_STAKE = 5.0  # $ - below this a test isn't worth it
SETTLE_HOURS = 3  # a bet is settled this long after kickoff: the match is over, stake + profit back on the balance
PLACED = ("ok", "ok_less", "miss", "unknown")  # statuses where money would really have been bet

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
    hedge_bookie: str = ""
    hedge_stake: float = 0.0
    hedge_odd: float = 0.0
    cap: float = 0.0  # the most the test was allowed to stake (total may be less: limits / thin book)
    free: float | None = None  # /bot balance left free after this bet (None = not tracked)
    settles: float | None = None  # /bot: when this bet's match is over and its profit goes onto the balance
    wallets: dict[str, float] | None = None  # /bot: money free on each bookie after this bet
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


def by_bookie(rows) -> dict[str, float]:
    """Stake per bookie of plan() rows (two legs can sit on the same bookie)."""
    out: dict[str, float] = {}
    for leg, stake, _ in rows:
        out[leg.bookie] = out.get(leg.bookie, 0.0) + stake
    return out


def fit_stake(arb: Arb, max_stake: float, currency: str = "$",
              caps: dict[str, float] | None = None, min_stake: float = MIN_STAKE) -> float | None:
    """The biggest total up to `max_stake` that this arb takes: the full amount if every leg
    fits, otherwise less (a bookie limit / thin Polymarket book). With `caps` (money on each
    bookie) no bookie's legs may need more than it has: if the split is 70/30 and each side
    has 25 $, the total is ~35 $, not 50 $. None = not even `min_stake`."""
    stake = max_stake
    if caps is not None:  # start where the tightest bookie runs out
        need: dict[str, float] = {}
        for leg, st in zip(arb.legs, arb.split(max_stake)):
            need[leg.bookie] = need.get(leg.bookie, 0.0) + st
        ratio = min((caps.get(b, 0.0) / st for b, st in need.items() if st > 0), default=1.0)
        stake = math.floor(max_stake * min(ratio, 1.0) * 2) / 2
    while stake >= min_stake - 1e-9:
        rows = arb.plan(stake, currency)
        if rows and (caps is None or all(st <= caps.get(b, 0.0) + 1e-9 for b, st in by_bookie(rows).items())):
            over = sum(st for _, st, _ in rows) - max_stake
            if over <= 1e-9:
                return stake
            stake -= math.ceil(over * 2) / 2  # rounded stakes went above the max: just that much lower
            continue
        # 100 -> 90 -> 81 ... on half-dollars (small stakes on dimes, and always at least one step down)
        stake = min(round(stake * 0.9 * 2) / 2 if stake >= 20 else round(stake * 0.9, 1), round(stake - 0.1, 1))
    return None


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


async def run_test(service, arb: Arb, s, currency: str, budget: float | None = None,
                   caps: dict[str, float] | None = None, delay: float = EXEC_DELAY) -> PaperResult:
    """One paper bet on this arb for user settings `s`, with total stake `budget`
    (default: the user's budget), no bookie above its `caps`, the last leg `delay` s after the first."""
    budget = s.budget if budget is None else budget
    t0 = time.perf_counter()
    ev = arb.event
    res = PaperResult(key=arb_key(arb), name=f"{ev.home} – {ev.away}",
                      market=market_label(arb.market, ev.sport, ev.home, ev.away),
                      sport=ev.sport, start=ev.start.isoformat(), status=GONE, cap=budget)
    # 1) odds right now on every leg (same check as clicking an arb)
    fresh = await service.verify([arb], s)
    if not fresh:
        res.seconds = time.perf_counter() - t0
        return res
    a = fresh[0]
    stake = fit_stake(a, budget, currency, caps)  # the fresh odds may take less than the scan's
    rows = a.plan(stake, currency) if stake else None
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
    await asyncio.sleep(delay)
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
        if e.bookie == last_leg.bookie or e.bookie not in s.bookies:
            continue  # only where the user can bet
        odd = e.markets.get(last_leg.market, {}).get(last_leg.outcome)
        if odd and (best is None or odd > best[0]):
            best = (odd, e.bookie)
    placed_stakes = sum(st for _, st, _ in rows[:-1])
    if best:
        # cover with a stake that pays the same as the bets already placed
        cover = min(placed) / best[0]
        res.hedge = f"{best[1]} @ {best[0]:g}, ulog {cover:.2f} $".replace(".", ",")
        res.hedge_bookie, res.hedge_stake, res.hedge_odd = best[1], cover, best[0]
        res.profit = min(placed) - placed_stakes - cover
    else:
        res.hedge = ""
        res.profit = -placed_stakes  # worst case: the uncovered side wins, every placed bet loses
    return res


# ---- storage ---------------------------------------------------------------

def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
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
                                            "hedge_bookie": r.hedge_bookie, "hedge_stake": r.hedge_stake,
                                            "hedge_odd": r.hedge_odd, "start": r.start, "sport": r.sport})))


def settles_at(start_iso: str) -> float | None:
    """time.time() when a bet on a match starting at `start_iso` counts as settled."""
    try:
        return datetime.fromisoformat(start_iso).timestamp() + SETTLE_HOURS * 3600
    except (TypeError, ValueError):
        return None


@dataclass
class Ledger:
    """The /bot balance: the starting money plus the profit of tested bets whose match
    is over. Bets on matches still to be played tie up their stake ("in play") and
    their profit waits - an arb pays out after the match, not when it is found.

    With money per bookie (`start`), every stake leaves its bookie when it is bet and,
    after the match, the whole return lands on the bookie of the leg that won - like
    for real, so the money piles up on one side. The test doesn't know real results:
    the winner is drawn by the odds (a 2.0 leg wins half the time)."""
    bank: float  # starting money in total
    start: dict[str, float] = field(default_factory=dict)  # starting money per bookie (empty = one shared budget)
    cash: dict[str, float] = field(default_factory=dict)  # money on each bookie now, not in play
    settled_profit: float = 0.0
    settled_n: int = 0
    in_play: float = 0.0
    pending_profit: float = 0.0
    open_n: int = 0
    next_settle: float | None = None  # when the first open bet settles

    @property
    def balance(self) -> float:
        return self.bank + self.settled_profit

    @property
    def free(self) -> float:
        return self.balance - self.in_play


def _flows(r: dict) -> tuple[dict[str, float], list[tuple[str, float]]]:
    """A stored test's stake per bookie, and the bets that can win: (bookie, odd)."""
    d = r["detail"]
    legs = d.get("legs") or []
    if not legs:  # nothing to split by bookie: the whole stake counts, on no bookie in particular
        return {"": r["total"]}, [("", 1.0)]
    spent: dict[str, float] = {}
    can_win: list[tuple[str, float]] = []
    for i, l in enumerate(legs):
        if i == len(legs) - 1 and r["status"] == MISS:  # the last leg was never bet...
            if d.get("hedge_bookie"):  # ...it was covered elsewhere
                b = d["hedge_bookie"]
                spent[b] = spent.get(b, 0.0) + d.get("hedge_stake", 0.0)
                can_win.append((b, d.get("hedge_odd") or l["odd"]))
            continue  # uncovered: if that side wins, nobody pays
        spent[l["bookie"]] = spent.get(l["bookie"], 0.0) + l["stake"]
        can_win.append((l["bookie"], l.get("now_odd") or l["odd"]))
    return spent, can_win


def _winner(r: dict, can_win: list[tuple[str, float]]) -> str | None:
    """The bookie whose bet won - drawn by the odds, the same every time for this test."""
    if not can_win:
        return None
    rng = random.Random(f"{r['key']}|{r['at']}")
    return rng.choices([b for b, _ in can_win], weights=[1 / o for _, o in can_win])[0]


def ledger(uid: int, bank: float, since: float = 0.0, now: float | None = None,
           start: dict[str, float] | None = None) -> Ledger:
    now = time.time() if now is None else now
    start = dict(start or {})
    out = Ledger(sum(start.values()) if start else bank, start, dict(start))
    for r in load(uid, since):
        if r["status"] not in PLACED:
            continue
        end = settles_at(r["detail"].get("start"))
        if end is None:
            continue
        spent, can_win = _flows(r)
        total = sum(spent.values())
        for b, st in spent.items():
            out.cash[b] = out.cash.get(b, 0.0) - st
        # "unknown": the last leg was never confirmed - its stake counts, no profit is assumed
        profit = 0.0 if r["status"] == UNKNOWN else r["profit"]
        if end <= now:
            out.settled_profit += profit
            out.settled_n += 1
            if (w := _winner(r, can_win)) is not None:
                out.cash[w] = out.cash.get(w, 0.0) + total + profit
        else:
            out.in_play += total
            out.pending_profit += profit
            out.open_n += 1
            out.next_settle = min(out.next_settle or end, end)
    return out


def rebalance(led: Ledger) -> tuple[str, str, float] | None:
    """(from, to, $) when one bookie has run low and another has more than its share
    (shares as in the starting money). None = no need to move anything."""
    if len(led.start) < 2:
        return None
    total = sum(max(led.cash.get(b, 0.0), 0.0) for b in led.start)
    weight = sum(led.start.values())
    if total < 2 * MIN_STAKE or not weight:
        return None
    target = {b: total * v / weight for b, v in led.start.items()}
    low = min(led.start, key=lambda b: led.cash.get(b, 0.0) / target[b] if target[b] else 1.0)
    if led.cash.get(low, 0.0) >= 0.5 * target[low]:
        return None
    high = max(led.start, key=lambda b: led.cash.get(b, 0.0) - target[b])
    amount = math.floor(min(target[low] - led.cash.get(low, 0.0), led.cash.get(high, 0.0) - target[high]))
    return (high, low, float(amount)) if high != low and amount >= MIN_STAKE else None


def load(uid: int, since: float) -> list[dict]:
    with _db() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM tests WHERE uid = ? AND at >= ? ORDER BY at", (uid, since)).fetchall()
    return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]
