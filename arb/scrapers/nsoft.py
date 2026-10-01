"""NSoft "Seven" sportsbook platform (Balkanbet). Public distribution API,
selected by the bookie's company UUID. rootEventId is the Betradar match id.

Market ids per sport:
  football   6 = 1X2 | 368 = double chance (hockey 147) | 443 = total goals ("0-2", "3+", ...) | 425 = GG/NG (+ combos)
  basketball 60 = winner incl. OT ("P1"/"P2") | 30 = 1X2 regular time
  tennis     1955 = match winner
  hockey     141 = 1X2 regular time
  table tennis 2060 = match winner ("P1"/"P2")"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from arb.models import SPORT_SLUGS, Event
from arb.scrapers.base import Scraper, make_client, slugify

API_URL = "https://sports-sm-distribution-api.de-2.nsoftcdn.com/api/v1/events"
DAYS_AHEAD = 60

# NSoft sportId -> (our sport, {market id: our market})
SPORTS = {
    18: ("football", {6: "1X2", 425: "BTTS", 368: "DC"}),
    36: ("basketball", {60: "12_OT", 30: "1X2"}),
    78: ("tennis", {1955: "12"}),
    21: ("hockey", {141: "1X2", 147: "DC"}),
    69: ("table_tennis", {2060: "12"}),
}
M_FOOTBALL_TOTAL = 443
TOTALS = {
    "0-1": ("OU_1.5", "U"), "2+": ("OU_1.5", "O"),
    "0-2": ("OU_2.5", "U"), "3+": ("OU_2.5", "O"),
    "0-3": ("OU_3.5", "U"), "4+": ("OU_3.5", "O"),
}
# "P1"/"P2" on the basketball winner market
OUTCOME_ALIASES = {"P1": "1", "P2": "2"}


class NSoftScraper(Scraper):
    company_uuid: str = ""
    site_url: str = ""

    def __init__(self) -> None:
        self.client = make_client({"Origin": "https://sports-sm-web.7platform.net"}, timeout=60)

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._fetch_sport(sid) for sid in SPORTS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, sport_id: int) -> list[Event]:
        sport, market_map = SPORTS[sport_id]
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S"
        r = await self.client.get(API_URL, params={
            "companyUuid": self.company_uuid,
            "deliveryPlatformId": 3,
            "language": json.dumps({"default": "sr-Latn"}),
            "timezone": "UTC",
            "dataFormat": json.dumps({"default": "array", "events": "array", "outcomes": "array"}),
            "filter[from]": now.strftime(fmt),
            "filter[to]": (now + timedelta(days=DAYS_AHEAD)).strftime(fmt),
            "filter[sportId]": sport_id,
        })
        r.raise_for_status()

        events = []
        for e in r.json()["data"]["events"]:
            if not e.get("active") or " - " not in (e.get("name") or ""):
                continue
            start = datetime.fromisoformat(e["startsAt"].replace("Z", "+00:00"))
            if start <= now:
                continue
            home, away = e["name"].split(" - ", 1)
            ev = Event(
                bookie=self.name,
                event_id=str(e["id"]),
                sport=sport,
                home=home.strip(),
                away=away.strip(),
                start=start,
                betradar_id=str(e["rootEventId"]) if e.get("rootEventId") else None,
                url=(f"{self.site_url}/sportsko-kladjenje/1-offer/{sport_id}-{SPORT_SLUGS[sport]}/"
                     f"{e['categoryId']}-x/{e['tournamentId']}-x/{e['id']}-{slugify(e['name'])}"),
            )
            markets = e.get("markets") or []
            if isinstance(markets, dict):
                markets = markets.values()
            for m in markets:
                if not m.get("active") or m.get("specialValues"):
                    continue
                mid = m.get("marketId")
                market = market_map.get(mid)
                for o in m.get("outcomes") or []:
                    if not o.get("active"):
                        continue
                    name = (o.get("name") or "").strip()
                    name = OUTCOME_ALIASES.get(name, name)
                    if market == "1X2" and name in ("1", "X", "2"):
                        ev.add("1X2", name, o.get("odd"))
                    elif market in ("12", "12_OT") and name in ("1", "2"):
                        ev.add(market, name, o.get("odd"))
                    elif market == "DC" and name in ("1X", "12", "X2"):
                        ev.add("DC", name, o.get("odd"))
                    elif market == "BTTS" and name in ("GG", "NG"):
                        ev.add("BTTS", name, o.get("odd"))
                    elif sport == "football" and mid == M_FOOTBALL_TOTAL and name in TOTALS:
                        ev.add(*TOTALS[name], o.get("odd"))
            if ev.markets:
                events.append(ev)
        return events

    async def close(self) -> None:
        await self.client.aclose()


class BalkanbetScraper(NSoftScraper):
    name = "Balkanbet"
    company_uuid = "4f54c6aa-82a9-475d-bf0e-dc02ded89225"
    site_url = "https://www.balkanbet.rs"
