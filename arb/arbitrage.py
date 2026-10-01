from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field

from arb.models import COMBOS, Event, market_outcomes

# Profit above this is almost always a mismatched market / wrong pairing / stale odd.
SUSPICIOUS_PROFIT = 15.0


@dataclass
class Leg:
    outcome: str
    odd: float
    bookie: str
    market: str = ""
    depth: list[tuple[float, float]] | None = None  # order book (see Event.depth), None = fixed odd
    limit: float | None = None  # most the bookie takes at this odd (Event.limits: Cloudbet, SX Bet)

    def payout(self, stake: float) -> float:
        """What `stake` returns if it wins. On an order book (Polymarket) a big stake
        also buys at the worse prices, so the average odd drops as the stake grows."""
        if not self.depth:
            return stake * self.odd
        left, out = stake, 0.0
        for odd, size in self.depth:
            take = min(left, size)
            out += take * odd
            left -= take
            if left <= 0:
                return out
        return out + left  # beyond the whole book: that part is not matched, the money stays

    @property
    def max_stake(self) -> float:
        """Most that can be placed on this leg: the bookie's limit / the whole order book."""
        caps = [self.limit] if self.limit is not None else []
        if self.depth:
            caps.append(sum(size for _, size in self.depth))
        return min(caps) if caps else float("inf")


@dataclass
class Arb:
    events: list[Event]
    market: str
    legs: list[Leg]
    margin: float  # sum(1/odd); < 1 means arbitrage
    checked_at: float = 0.0  # time.time() when the legs' odds were re-fetched (0 = from the scan)
    # after a re-check (ArbService.verify): bookies that answered just now, and each leg's odd before it
    fresh: set[str] | None = None
    prev_odds: dict[tuple[str, str], float] = field(default_factory=dict)
    _plans: dict = field(default_factory=dict, repr=False, compare=False)  # (budget, currency) -> plan()

    @property
    def event(self) -> Event:
        return self.events[0]

    def event_for(self, bookie: str) -> Event | None:
        return next((e for e in self.events if e.bookie == bookie), None)

    @property
    def profit_pct(self) -> float:
        return (1 / self.margin - 1) * 100

    @property
    def suspicious(self) -> bool:
        return self.profit_pct > SUSPICIOUS_PROFIT

    @property
    def has_depth(self) -> bool:
        return any(l.depth for l in self.legs)

    def split(self, total: float) -> list[float]:
        """Stakes that make every outcome pay the same. With an order book the odd
        depends on the stake, so the split is repeated with the average odds."""
        odds = [l.odd for l in self.legs]
        for _ in range(8 if self.has_depth else 1):
            inv = sum(1 / o for o in odds)
            stakes = [total * (1 / o) / inv for o in odds]
            odds = [l.payout(s) / s if s > 0 else l.odd for l, s in zip(self.legs, stakes)]
        return stakes

    def profit_at(self, total: float) -> float:
        """Guaranteed profit (fraction of `total`) when `total` is split exactly."""
        return min(l.payout(s) for l, s in zip(self.legs, self.split(total))) / total - 1

    def pct_at(self, total: float) -> float:
        """Profit % for this total stake (= profit_pct unless a leg is an order book)."""
        return self.profit_at(total) * 100 if self.has_depth else self.profit_pct

    def plan(self, budget: float, currency: str = "din") -> list[tuple[Leg, float, float]] | None:
        """Rounded stakes for this budget - or None when this arb doesn't work for it:
        a leg needs more than its bookie takes at that odd (limit / order book), or
        the stakes don't make a profit (Polymarket's odd drops with a bigger stake).
        Such an arb is simply not shown for this budget."""
        key = (budget, currency)
        if key not in self._plans:
            rows = self.rounded_stakes(budget, currency)
            total = sum(s for _, s, _ in rows)
            fits = all(s <= leg.max_stake + 1e-9 for leg, s, _ in rows)
            self._plans[key] = rows if fits and total and min(p for _, _, p in rows) > total else None
        return self._plans[key]

    def pct_for(self, budget: float, currency: str = "din") -> float:
        """Real profit % of plan() for this budget (-100 if it doesn't work for it)."""
        rows = self.plan(budget, currency)
        if not rows:
            return -100.0
        total = sum(s for _, s, _ in rows)
        return (min(p for _, _, p in rows) / total - 1) * 100

    def stakes(self, total: float, round_to: float = 10) -> list[tuple[Leg, float, float]]:
        """Split `total` so every outcome pays (about) the same. Returns (leg, stake, payout)."""
        out = []
        for leg, stake in zip(self.legs, self.split(total)):
            if round_to:
                stake = round(stake / round_to) * round_to
            out.append((leg, stake, leg.payout(stake)))
        return out

    def rounded_stakes(self, budget: float, currency: str = "din") -> list[tuple[Leg, float, float]]:
        """Stakes rounded to amounts a normal player would pick (…4.300, not 4.263),
        so the bets don't look calculated. The total may go up to MAX_OVER_BUDGET
        above the budget. Picks the rounding that keeps the best guaranteed return;
        falls back to 10-din steps if no round combination stays profitable."""
        best, best_key = None, None
        for scale in (1.0, 1.01, 1.02, 1.03):
            options = []
            for leg, ideal in zip(self.legs, self.split(budget * scale)):
                step = _round_step(ideal, currency)
                low = math.floor(ideal / step) * step
                options.append({max(low, step), low + step})
            for combo in itertools.product(*options):
                total = sum(combo)
                if total > budget * (1 + MAX_OVER_BUDGET):
                    continue
                profit = min(leg.payout(s) for s, leg in zip(combo, self.legs)) - total
                fits = all(s <= leg.max_stake for s, leg in zip(combo, self.legs))
                key = (fits, profit > 0, profit / total, -abs(total - budget))
                if best_key is None or key > best_key:
                    best, best_key = combo, key
        if best is None or best_key[2] <= 0:
            return self.stakes(budget, round_to=(0.1 if budget < 50 else 0.5) if currency == "$" else 10)
        return [(leg, s, leg.payout(s)) for leg, s in zip(self.legs, best)]


MAX_OVER_BUDGET = 0.03  # rounded total may exceed the budget by up to 3 %


def _round_step(stake: float, currency: str = "din") -> float:
    """Rounding step that looks natural for a stake of this size."""
    if currency == "$":  # crypto sites take cents, so half-dollars are fine (2.5, 10.5 ...)
        return 0.1 if stake < 50 else 0.5 if stake < 100 else 1 if stake < 500 else 5 if stake < 5_000 else 10
    if stake < 2_000:
        return 50
    if stake < 20_000:
        return 100
    if stake < 100_000:
        return 500
    return 1_000


def _best_legs(grp: list[Event], picks) -> list[Leg] | None:
    """Best odd for every (market, outcome) pick; None if one is missing everywhere."""
    legs = []
    if not grp:
        return None
    for market, oc in picks:
        odd, ev = max(
            ((ev.markets.get(market, {}).get(oc), ev) for ev in grp),
            key=lambda x: x[0] or 0,
        )
        if not odd:
            return None
        legs.append(Leg(oc, odd, ev.bookie, market, ev.depth.get((market, oc)), ev.limits.get((market, oc))))
    return legs


MAX_PROB_GAP = 0.15  # how far one bookie's (fair) probability may sit from the others'
FAVOURITE_GAP = 0.08  # a clear favourite (fair probabilities this far apart) must be the same side everywhere


def _outliers(grp: list[Event], market: str, outcomes: tuple[str, ...]) -> set[str]:
    """Bookies whose prices for `market` contradict the rest of the group.

    A real price difference is a few percentage points of probability; 30+ points
    means that bookie is quoting another match (women's vs men's side, wrong pairing)
    or has home/away the other way round. Those must never become arb legs."""
    fair: dict[str, dict[str, float]] = {}
    for ev in grp:
        prices = ev.markets.get(market, {})
        if all(o in prices for o in outcomes):
            total = sum(1 / prices[o] for o in outcomes)
            fair[ev.bookie] = {o: (1 / prices[o]) / total for o in outcomes}
    if not fair:
        return set()
    if len(fair) == 2:
        a, b = fair.values()
        if any(abs(a[o] - b[o]) > MAX_PROB_GAP for o in outcomes):
            return {ev.bookie for ev in grp}  # two books disagree wildly: can't tell who is right
    median = {o: statistics.median(p[o] for p in fair.values()) for o in outcomes}
    bad = {b for b, p in fair.items() if any(abs(p[o] - median[o]) > MAX_PROB_GAP for o in outcomes)}
    if "1" in outcomes and "2" in outcomes and len(fair) >= 3:
        # the favourite turned round: that book's feed has home/away the other way (names say one
        # thing, odds the other) - e.g. 2.05 / 3.10 at three books, 3.00 / 2.10 at eight others
        lean = median["1"] - median["2"]
        if abs(lean) >= FAVOURITE_GAP:
            bad |= {b for b, p in fair.items() if (p["1"] - p["2"]) * lean < 0 and abs(p["1"] - p["2"]) >= FAVOURITE_GAP}
    # books with only part of the market: compare the raw implied probability (margin allowance)
    for ev in grp:
        if ev.bookie in fair:
            continue
        prices = ev.markets.get(market, {})
        if any(abs(1 / prices[o] - median[o]) > MAX_PROB_GAP + 0.05 for o in outcomes if o in prices):
            bad.add(ev.bookie)
    return bad


def find_arbs(groups: list[list[Event]], min_profit: float = 0.0) -> list[Arb]:
    arbs = []
    for grp in groups:
        if len(grp) < 2:
            continue
        # every market some book of this match offers (lines differ per match)
        keys = sorted({k for ev in grp for k in ev.markets if not k.endswith("DC")})
        outcomes_of = {k: market_outcomes(k) for k in keys}
        candidates = [(m, [(m, oc) for oc in outs]) for m, outs in outcomes_of.items() if outs]
        for p in ("", "H1_"):  # 1 + X2 etc., full time and first half
            if p + "1X2" in outcomes_of and any(p + "DC" in ev.markets for ev in grp):
                candidates += [(p + name, [(p + m, oc) for m, oc in picks]) for name, picks in COMBOS.items()]
        bad = {m: _outliers(grp, m, outs) for m, outs in outcomes_of.items() if outs}
        for market, picks in candidates:
            used = {m for m, _ in picks}
            # double chance is judged by the same book's 1X2 (DC alone has no fair form)
            excluded = set().union(*(bad.get(m[:-2] + "1X2" if m.endswith("DC") else m, set()) for m in used))
            usable = [ev for ev in grp if ev.bookie not in excluded]
            if len(usable) < 2:  # e.g. the only two books disagree wildly -> both excluded
                continue
            legs = _best_legs(usable, picks)
            if not legs or len({l.bookie for l in legs}) < 2:
                continue
            margin = sum(1 / l.odd for l in legs)
            arb = Arb(grp, market, legs, margin)
            if margin < 1 and arb.profit_pct >= min_profit:
                arbs.append(arb)
    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs
