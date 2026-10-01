"""Middles ("srednjice"): two bookies, two different lines, a result window where
BOTH bets win.

  Over 2.5 @ A  +  Under 3.5 @ B   -> exactly 3 goals: both win; otherwise one wins.
  Home -0.5 @ A +  Away +1.5 @ B   -> home wins by exactly 1: both win.

Stakes are split so either single win pays the same. Outside the window you lose a
little (or win, when the odds alone already make an arb); inside it you win almost
double. Not a sure profit - shown separately from arbs, with the break-even chance.
Only .5 lines: whole lines add pushes and make the window ambiguous."""
from __future__ import annotations

import math
from dataclasses import dataclass

from arb.arbitrage import Leg
from arb.models import Event, split_market

MAX_LOSS = 0.05  # skip pairs that lose more than 5 % of the stake when the middle misses
MAX_WINDOW_LINES = 6  # e.g. Over 160.5 / Under 166.5 in basketball - wider is suspicious


@dataclass
class Middle:
    events: list[Event]
    kind: str  # "OU" (total) or "AH" (handicap)
    prefix: str  # "", "H1_", "T1_", "T2_"
    low: float  # Over line / home handicap of the home leg
    high: float  # Under line / home handicap of the away leg
    legs: list[Leg]  # [over-or-home leg, under-or-away leg]
    chance: float | None = None  # estimated probability of landing in the window (from fair prices)

    @property
    def event(self) -> Event:
        return self.events[0]

    def event_for(self, bookie: str) -> Event | None:
        return next((e for e in self.events if e.bookie == bookie), None)

    @property
    def inv(self) -> float:
        return sum(1 / l.odd for l in self.legs)

    @property
    def miss(self) -> float:
        """Profit (fraction of the stake) when only one bet wins (negative = loss)."""
        return 1 / self.inv - 1

    @property
    def hit(self) -> float:
        """Profit (fraction of the stake) when the middle lands and both win."""
        return 2 / self.inv - 1

    @property
    def breakeven(self) -> float:
        """Chance the middle must have to be worth it (0 = profitable anyway)."""
        if self.miss >= 0:
            return 0.0
        return -self.miss / (self.hit - self.miss)

    @property
    def ev(self) -> float | None:
        """Expected profit (fraction of the stake) with the estimated window chance."""
        if self.chance is None:
            return None
        return self.chance * self.hit + (1 - self.chance) * self.miss

    @property
    def window(self) -> list[int]:
        """Totals (OU) or home winning margins (AH) for which both bets win."""
        if self.kind == "OU":
            return list(range(math.floor(self.low) + 1, math.ceil(self.high)))
        # home covers when margin > -low, away covers when margin < -high
        return list(range(math.floor(-self.low) + 1, math.ceil(-self.high)))

    @property
    def key(self) -> str:
        return f"{self.event.group}:{self.prefix}{self.kind}:{self.low:g}:{self.high:g}"


def _half(x: float) -> bool:
    return (x * 2) % 2 == 1  # .5 line


def _fair_over(grp: list[Event], mk_prefix: str, kind: str, line: float) -> float | None:
    """Median de-vigged probability of Over (OU) / home covering (AH) at this line."""
    key = f"{mk_prefix}{kind}_{line:g}"
    a, b = ("O", "U") if kind == "OU" else ("1", "2")
    probs = []
    for ev in grp:
        o = ev.markets.get(key, {})
        if a in o and b in o:
            pa, pb = 1 / o[a], 1 / o[b]
            probs.append(pa / (pa + pb))
    if not probs:
        return None
    probs.sort()
    return probs[len(probs) // 2]


def find_middles(groups: list[list[Event]], max_loss: float = MAX_LOSS) -> list[Middle]:
    out: list[Middle] = []
    for grp in groups:
        if len(grp) < 2:
            continue
        # best price per (prefix, kind, line, side) across the group's bookies
        best: dict[tuple, tuple[float, str, str]] = {}
        for ev in grp:
            for mk, outs in ev.markets.items():
                period, team, base = split_market(mk)
                if not base.startswith(("OU_", "AH_")):
                    continue
                prefix = (period + "_" if period else "") + (team + "_" if team else "")
                kind, line = base[:2], float(base[3:])
                if not _half(line):
                    continue
                for oc, odd in outs.items():
                    k = (prefix, kind, line, oc)
                    if k not in best or odd > best[k][0]:
                        best[k] = (odd, ev.bookie, mk)
        lines: dict[tuple, set[float]] = {}
        for prefix, kind, line, _ in best:
            lines.setdefault((prefix, kind), set()).add(line)
        for (prefix, kind), ls in lines.items():
            ls = sorted(ls)
            for lo in ls:
                for hi in ls:
                    if kind == "OU" and not (lo < hi <= lo + MAX_WINDOW_LINES):
                        continue
                    if kind == "AH" and not (hi < lo <= hi + MAX_WINDOW_LINES):
                        continue
                    a = best.get((prefix, kind, lo, "O" if kind == "OU" else "1"))
                    b = best.get((prefix, kind, hi, "U" if kind == "OU" else "2"))
                    if not a or not b or a[1] == b[1]:
                        continue
                    legs = [Leg("O" if kind == "OU" else "1", a[0], a[1], a[2]),
                            Leg("U" if kind == "OU" else "2", b[0], b[1], b[2])]
                    m = Middle(grp, kind, prefix, lo, hi, legs)
                    if m.miss < -max_loss or not m.window:
                        continue
                    p_lo, p_hi = _fair_over(grp, prefix, kind, lo), _fair_over(grp, prefix, kind, hi)
                    if p_lo is not None and p_hi is not None:
                        m.chance = max(0.0, p_lo - p_hi)  # P(over lo) - P(over hi) = P(in between)
                    out.append(m)
    # best expected value first (unknown chance last), one pair per match/market
    out.sort(key=lambda m: (m.ev is None, -(m.ev or 0), m.breakeven))
    seen, unique = set(), []
    for m in out:
        if m.ev is not None and m.ev <= 0:
            continue  # the books' own prices say this middle loses on average
        k = (m.event.group, m.prefix, m.kind)
        if k in seen:
            continue
        seen.add(k)
        unique.append(m)
    return unique
