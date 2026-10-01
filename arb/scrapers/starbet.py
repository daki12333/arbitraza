"""StarBet: ASP.NET page methods (Oblozuvanje.aspx). No Betradar id, so its
matches are paired by team names and kickoff time."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

BASE = "https://www.starbet.rs"
SPORTS = {0: "football", 22: "basketball", 37: "tennis", 8: "hockey", 7: "handball", 38: "volleyball"}
# TID -> (market, outcome); football extras below
TIDS_1X2 = {1: ("1X2", "1"), 2: ("1X2", "X"), 10: ("1X2", "2")}
TIDS_DC = {83: ("DC", "1X"), 85: ("DC", "X2")}
TIDS = {
    "football": {**TIDS_1X2, **TIDS_DC, 70: ("OU_2.5", "U"), 74: ("OU_2.5", "O"), 112: ("BTTS", "GG")},
    "basketball": TIDS_1X2,  # "Konačan ishod" = regular time 1X2
    "tennis": {1: ("12", "1"), 10: ("12", "2")},
    "hockey": {**TIDS_1X2, **TIDS_DC},
    "handball": TIDS_1X2,
    "volleyball": {1: ("12", "1"), 10: ("12", "2")},
}


def _parse_time(s: str) -> datetime:
    # "2026-10-13T18:45:00.0000000+02:00" - 7 fractional digits
    return datetime.fromisoformat(re.sub(r"\.\d+", "", s)).astimezone(timezone.utc)


class StarBetScraper(Scraper):
    name = "StarBet"

    def __init__(self) -> None:
        self.client = make_client({
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/Bet",
        }, timeout=90)

    async def fetch(self) -> list[Event]:
        r = await self.client.post(f"{BASE}/Oblozuvanje.aspx/GetSportoviSoLigi",
                                   content=json.dumps({"filter": "0", "activeStyle": ""}))
        r.raise_for_status()
        league_ids = {s["SID"]: [lg["LID"] for lg in s.get("L") or []] for s in r.json() if s["SID"] in SPORTS}

        now = datetime.now(timezone.utc)
        events = []
        for sid, ids in league_ids.items():
            if not ids:
                continue
            sport = SPORTS[sid]
            r = await self.client.post(f"{BASE}/Oblozuvanje.aspx/GetLiga",
                                       content=json.dumps({"LigaID": ids, "filter": "0", "parId": 0}))
            r.raise_for_status()
            for lg in r.json():
                for p in lg.get("P") or []:
                    teams = (p.get("PN") or "").split(" : ")
                    if len(teams) != 2:
                        continue
                    start = _parse_time(p["DI"])
                    if start <= now:
                        continue
                    ev = Event(
                        bookie=self.name,
                        event_id=str(p["PID"]),
                        sport=sport,
                        home=teams[0].strip(),
                        away=teams[1].strip(),
                        start=start,
                        league=lg.get("LN") or "",
                        # the site's own search opens a match with ?p=<match>&l=<league>
                        url=f"{BASE}/Bet?p={p['PID']}&l={lg.get('LID')}",
                    )
                    for t in p.get("T") or []:
                        m = TIDS[sport].get(t.get("TID"))
                        if m and not t.get("E"):
                            ev.add(*m, t.get("K"))
                    if ev.markets:
                        events.append(ev)
        return events

    async def close(self) -> None:
        await self.client.aclose()
