"""📈 Procena: how many SX Bet + Polymarket arbs there really are and what they would make.

Every scan writes the pair's arbs to the book (table "seen"): the same arb with the same
odds is one row, with when it was first and last seen, its profit % and the most it takes
($, the order books' depth / SX's size at the best price). /bot → 📈 Procena turns the last
24 h of that into a daily estimate for the user's stake and capital.

It is an upper bound: it counts every arb as caught. The real rate (an arb gone before the
bet, a Polymarket book that moved) shows up in the tickets once the bot plays for real, or
in /bottest with money set on SX Bet + Polymarket."""
from __future__ import annotations

import statistics
import time

from arb.arbitrage import Arb

TIERS = (5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000)  # $ totals tried for "the most it takes"
WINDOW = 24 * 3600


def capacity(a: Arb) -> float:
    """The biggest total from TIERS that this arb still takes with a profit (0 = not even 5 $)."""
    best = 0.0
    for t in TIERS:
        if a.plan(t, "$") is None:
            break
        best = t
    return best


def record(book, arbs: list[Arb]) -> None:
    from arb.live.engine import is_pair, leg_ref
    from arb.tg.service import arb_key

    rows = []
    for a in arbs:
        if not is_pair(a) or a.suspicious or any(not leg_ref(a, l) for l in a.legs):
            continue
        ev = a.event
        sig = ";".join(f"{l.bookie}:{l.outcome}:{l.odd}" for l in a.legs)
        rows.append((arb_key(a), sig, ev.sport, f"{ev.home} – {ev.away}", a.market, ev.start.isoformat(),
                     round(a.profit_pct, 3), capacity(a)))
    if rows:
        book.record_seen(rows)


def estimate(book, stake: float, min_pct: float, capital: float | None) -> dict | None:
    """The last 24 h (or less, since tracking started) as one day. None = nothing recorded yet."""
    now = time.time()
    first = book.seen_first()
    if first is None:
        return None
    hours = min(WINDOW, now - first) / 3600
    rows = book.seen_since(now - WINDOW)
    best: dict[str, dict] = {}  # per arb (match + market): its best moment
    for r in rows:
        if r["cap"] <= 0:
            continue
        b = best.get(r["arb_key"])
        if b is None or r["pct"] * min(stake, r["cap"]) > b["pct"] * min(stake, b["cap"]):
            best[r["arb_key"]] = r
    for k, r in best.items():
        same = [x for x in rows if x["arb_key"] == k]
        r["lasted"] = max(x["last_at"] for x in same) - min(x["first_at"] for x in same)
    good = [r for r in best.values() if r["pct"] >= min_pct]
    scale = 24 / hours if hours > 0 else 0
    profit = sum(min(stake, r["cap"]) * r["pct"] / 100 for r in good)
    sports: dict[str, int] = {}
    for r in good:
        sports[r["sport"]] = sports.get(r["sport"], 0) + 1
    return {
        "hours": hours,
        "all": len(best),
        "good": len(good),
        "per_day": len(good) * scale,
        "median_pct": statistics.median(r["pct"] for r in good) if good else 0.0,
        "median_cap": statistics.median(r["cap"] for r in good) if good else 0.0,
        "median_lasted": statistics.median(r["lasted"] for r in good) if good else 0.0,
        "staked_per_day": sum(min(stake, r["cap"]) for r in good) * scale,
        "profit_per_day": profit * scale,
        "pct_of_capital": profit * scale / capital * 100 if capital else None,
        "sports": sorted(sports.items(), key=lambda x: -x[1]),
        "top": sorted(good, key=lambda r: -r["pct"] * min(stake, r["cap"]))[:5],
    }
