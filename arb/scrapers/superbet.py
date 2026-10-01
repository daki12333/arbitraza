"""Superbet: public offer API on Fastly. The by-date list only carries each
sport's main market (football/hockey 1X2, tennis winner, basketball winner incl.
OT) - other markets would need one request per event."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from arb.models import SPORT_SLUGS, Event
from arb.scrapers.base import Scraper, make_client, slugify

URL = "https://production-superbet-offer-rs.freetls.fastly.net/sb-rs/api/v2/sr-Latn-RS/events/by-date"
DAYS_AHEAD = 60
# Superbet sportId -> (our sport, main market id, our market)
SPORTS = {
    5: ("football", 547, "1X2"),
    4: ("basketball", 759, "12_OT"),  # "Pobednik (uklj. produžetke)"
    2: ("tennis", 521, "12"),
    3: ("hockey", 640, "1X2"),
    11: ("handball", 869, "1X2"),
    1: ("volleyball", 745, "12"),
    24: ("table_tennis", 919, "12"),
}


class SuperbetScraper(Scraper):
    name = "Superbet"

    def __init__(self) -> None:
        self.client = make_client({"Referer": "https://superbet.rs/"}, timeout=60)

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._fetch_sport(sid) for sid in SPORTS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, sport_id: int) -> list[Event]:
        sport, market_id, market = SPORTS[sport_id]
        outcomes = ("1", "X", "2") if market == "1X2" else ("1", "2")
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S"
        r = await self.client.get(URL, params={
            "offerState": "prematch",
            "startDate": now.strftime(fmt),
            "endDate": (now + timedelta(days=DAYS_AHEAD)).strftime(fmt),
            "sportId": sport_id,
        })
        r.raise_for_status()

        events = []
        for e in r.json().get("data") or []:
            teams = (e.get("matchName") or "").split("·")
            if len(teams) != 2 or (e.get("offerStateStatus") or {}).get("1") != "active":
                continue
            start = datetime.fromisoformat(e["utcDate"].replace("Z", "+00:00"))
            if start <= now:
                continue
            home, away = teams[0].strip(), teams[1].strip()
            ev = Event(
                bookie=self.name,
                event_id=str(e["eventId"]),
                sport=sport,
                home=home,
                away=away,
                start=start,
                betradar_id=str(e["betradarId"]) if e.get("betradarId") else None,
                url=f"https://superbet.rs/kvote/{SPORT_SLUGS[sport]}/{slugify(home)}-vs-{slugify(away)}-{e['eventId']}",
            )
            for o in e.get("odds") or []:
                if o.get("marketId") == market_id and o.get("status") == "active" and o.get("name") in outcomes:
                    ev.add(market, o["name"], o.get("price"))
            if ev.markets:
                events.append(ev)
        return events

    async def close(self) -> None:
        await self.client.aclose()
