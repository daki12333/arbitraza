"""Run one scan from the terminal:  python scan.py [--stake 10000] [--min 0] [--dump]"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from arb.models import MARKET_LABELS, outcome_label
from arb.scanner import Scanner

LOCAL_TZ = ZoneInfo("Europe/Belgrade")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stake", type=float, default=10_000)
    ap.add_argument("--min", type=float, default=0.0, help="minimalni profit u %%")
    ap.add_argument("--dump", action="store_true", help="sačuvaj sve kvote u data/snapshot.json")
    args = ap.parse_args()

    scanner = Scanner()
    try:
        res = await scanner.scan(args.min)
    finally:
        await scanner.close()

    print("\n=== KLADIONICE ===")
    for name, r in res.bookies.items():
        n_br = sum(1 for e in r.events if e.betradar_id)
        mk = Counter(e.sport for e in r.events)
        status = f"GREŠKA: {r.error}" if r.error else f"{len(r.events):4d} mečeva ({n_br} sa BR id)"
        print(f"{name:10s} {r.seconds:5.1f}s  {status}  {dict(mk)}")

    sizes = Counter(len(g) for g in res.groups)
    print("\n=== UPARIVANJE ===")
    for k in sorted(sizes, reverse=True):
        print(f"  mečeva ponuđenih u {k} kladionica: {sizes[k]}")

    print(f"\n=== ARBITRAŽE ({len(res.arbs)}) ===")
    for a in res.arbs[:30]:
        ev = a.event
        start = ev.start.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
        flag = "  ⚠️ SUMNJIVO" if a.suspicious else ""
        print(f"\n{ev.name} | {start} | {ev.league}")
        print(f"  {MARKET_LABELS[a.market]}  profit {a.profit_pct:.2f}%{flag}")
        for leg, stake, payout in a.stakes(args.stake):
            print(f"    {outcome_label(a.market, leg.outcome):>4} @ {leg.odd:<6} {leg.bookie:10s} ulog {stake:>8,.0f}  isplata {payout:>9,.0f}")
        names = {e.bookie: e.name for e in a.events}
        if len(set(names.values())) > 1:
            print("    nazivi:", names)

    if args.dump:
        Path("data").mkdir(exist_ok=True)
        dump = [
            [{"bookie": e.bookie, "name": e.name, "start": e.start.isoformat(), "br": e.betradar_id,
              "league": e.league, "markets": e.markets} for e in g]
            for g in res.groups
        ]
        Path("data/snapshot.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\nSačuvano u data/snapshot.json")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
