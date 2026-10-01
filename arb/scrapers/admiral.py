from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

DAYS_AHEAD = 60
OU_LINES = {"1.5", "2.5", "3.5"}

# Admiral sportId -> our sport, and betTypeId -> market for the plain (no line) markets
SPORTS = {1: "football", 2: "basketball", 3: "tennis", 4: "hockey", 6: "handball"}
PLAIN_BET_TYPES = {
    "football": {135: "1X2", 1380: "BTTS", 152: "DC"},
    "basketball": {186: "12_OT", 189: "1X2"},  # "Pobednik" / "Konacan ishod" (regular time)
    "tennis": {1723: "12"},
    "hockey": {135: "1X2", 152: "DC"},
    "handball": {135: "1X2"},
}
BT_FOOTBALL_TOTAL = 137


class AdmiralScraper(Scraper):
    """Admiral's "WebBet" platform."""

    name = "Admiral"
    offer_api = "https://srboffer.admiralbet.rs"
    office_id = "138"
    site_url = "https://admiralbet.rs"

    def __init__(self) -> None:
        self.client = make_client(
            {"Language": "sr-Latn", "OfficeId": self.office_id, "Referer": self.site_url + "/"}, timeout=60
        )

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._fetch_sport(sid) for sid in SPORTS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, sport_id: int) -> list[Event]:
        sport = SPORTS[sport_id]
        plain = PLAIN_BET_TYPES[sport]
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000"
        params = [
            ("pageId", "3"),
            ("sportId", str(sport_id)),
            ("isLive", "false"),
            ("dateFrom", now.strftime(fmt)),
            ("dateTo", (now + timedelta(days=DAYS_AHEAD)).strftime(fmt)),
        ] + [("eventMappingTypes", str(i)) for i in range(1, 6)]
        r = await self.client.get(f"{self.offer_api}/api/offer/getWebEventsSelections", params=params)
        r.raise_for_status()

        events = []
        for e in r.json():
            # eventTypeId 2 = outright ("Engleska 1 26/27"), 1 = match
            if e.get("eventTypeId") != 1 or e.get("isLive") or " - " not in e["name"]:
                continue
            home, away = e["name"].split(" - ", 1)
            ev = Event(
                bookie=self.name,
                event_id=str(e["id"]),
                sport=sport,
                home=home.strip(),
                away=away.strip(),
                start=datetime.fromisoformat(e["dateTime"]).replace(tzinfo=timezone.utc),
                league=e.get("competitionName") or "",
                betradar_id=str(e["betRadarEventId"]) if e.get("betRadarEventId") else None,
                url=self.match_url(e),
            )
            for b in e.get("bets") or []:
                if not b.get("isPlayable", True):
                    continue
                bt = b["betTypeId"]
                for o in b.get("betOutcomes") or []:
                    if not o.get("isPlayable", True):
                        continue
                    name = (o.get("name") or "").strip().lower()
                    market = plain.get(bt) if not b.get("sbv") else None
                    if market in ("1X2", "12", "12_OT") and name in ("1", "x", "2"):
                        if market != "1X2" and name == "x":
                            continue
                        ev.add(market, name.upper(), o["odd"])
                    elif market == "DC" and name in ("1x", "12", "x2"):
                        ev.add("DC", name.upper(), o["odd"])
                    elif market == "BTTS" and name in ("gg", "ng"):
                        ev.add("BTTS", name.upper(), o["odd"])
                    elif sport == "football" and bt == BT_FOOTBALL_TOTAL and b.get("sbv") in OU_LINES:
                        side = {"manje": "U", "vise": "O"}.get(name)
                        if side:
                            ev.add(f"OU_{b['sbv']}", side, o["odd"])
            if ev.markets:
                events.append(ev)
        return events

    def match_url(self, e: dict) -> str:
        # The site needs the full region/competition path, not just the event id.
        comp = (e.get("competitionName") or "").strip()
        return f"{self.site_url}/sport-prematch?" + urlencode({
            "sport": (e.get("sportName") or "Fudbal").strip(),
            "region": (e.get("regionName") or "").strip().replace(" ", "_"),
            "competitionId": f"{e['sportId']}-{e['regionId']}-{e['competitionId']}",
            "competition": comp.replace(" ", "_"),
            "event": e["id"],
            "eventName": e["name"].strip().replace(" ", "_"),
        })

    async def close(self) -> None:
        await self.client.aclose()

