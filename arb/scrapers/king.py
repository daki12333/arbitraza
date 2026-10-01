"""King.rs: its sportsbook is Phoenix365 ("dante" partner), embedded as an iframe.
Public JSON API: listing/tournaments-with-events gives the matches (paged by
tournament); events/main-markets gives the odds for many events at once.

Market type ids: football 77 = 1X2, 23 = double chance, 103 = total (main line
only) | basketball 151 = winner incl. OT | tennis 177, volleyball 185, table
tennis 228 = winner | handball 201 = 1X2. Hockey only offers "winner incl. OT",
which nobody else prices, so it is skipped."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from arb.models import Event, utc_from_ms
from arb.scrapers.base import Scraper, make_client, slugify

API = "https://danteprod.phoenix365-prod.com/partner-api/sportsbook/public/v2"
SITE = "https://king.rs/sport/sport"
PAGE = 100  # tournaments per listing page
BATCH = 150  # events per main-markets call

# King sportId -> (our sport, URL slug, {market type id: our market})
SPORTS = {
    "1": ("football", "football", {"77": "1X2", "23": "DC", "103": "OU"}),
    "2": ("basketball", "basketball", {"151": "12_OT"}),
    "3": ("tennis", "tennis", {"177": "12"}),
    "4": ("volleyball", "volleyball", {"185": "12"}),
    "6": ("handball", "handball", {"201": "1X2"}),
    "10": ("table_tennis", "table-tennis", {"228": "12"}),
}
OUTCOMES = {"3": "1", "18": "X", "34": "2", "4": "1X", "19": "12", "35": "X2", "2": "O", "16": "U"}
OU_LINES = {"1.5", "2.5", "3.5"}


class KingScraper(Scraper):
    name = "King"

    def __init__(self) -> None:
        self.client = make_client({"Referer": "https://danteprod.phoenix365-prod.com/"}, timeout=60)

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._fetch_sport(sid) for sid in SPORTS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, sport_id: str) -> list[Event]:
        sport, slug, market_map = SPORTS[sport_id]
        now = datetime.now(timezone.utc)
        events: dict[str, Event] = {}
        offset = 0
        while True:
            r = await self.client.get(f"{API}/listing/tournaments-with-events", params={
                "sportId": sport_id, "sportService": "PREMATCH", "offset": offset, "limit": PAGE,
                "onlyTopTournaments": "false",
            })
            r.raise_for_status()
            tournaments = r.json().get("tournaments") or []
            for t in tournaments:
                for e in t.get("events") or []:
                    ev = self._event(e, sport, slug, now)
                    if ev:
                        events[e["eventId"]] = ev
            if len(tournaments) < PAGE:
                break
            offset += PAGE

        ids = list(events)
        type_ids = ",".join(market_map)
        await asyncio.gather(*(self._odds(ids[i:i + BATCH], type_ids, market_map, events)
                               for i in range(0, len(ids), BATCH)))
        return [ev for ev in events.values() if ev.markets]

    def _event(self, e: dict, sport: str, slug: str, now: datetime) -> Event | None:
        if e.get("isOutright") or e.get("eventStatus") != "NOT_STARTED":
            return None
        sides = {p.get("qualifier"): p.get("fullName") for p in e.get("participants") or []}
        if not sides.get("home") or not sides.get("away"):
            return None
        start = utc_from_ms(e["startTime"])
        if start <= now:
            return None
        return Event(
            bookie=self.name,
            event_id=str(e["eventId"]),
            sport=sport,
            home=sides["home"],
            away=sides["away"],
            start=start,
            league=e.get("tournamentName") or "",
            # matchTrackerInfo.betradarId is often another match's id (seen: "Norway -
            # Portugal" carrying "Denmark - Portugal"'s id), so King is paired by name
            betradar_id=None,
            # king.rs mirrors the sportsbook's own path; it needs the real slugs
            url=(f"{SITE}/{slug}/{slugify(e.get('regionName') or '')}/{slugify(e.get('tournamentName') or '')}/"
                 f"{slugify(sides['home'])}-vs-{slugify(sides['away'])}-{e['eventId']}"),
        )

    async def _odds(self, ids: list[str], type_ids: str, market_map: dict, events: dict[str, Event]) -> None:
        r = await self.client.get(f"{API}/events/main-markets", params={"marketTypeIds": type_ids, "eventIds": ",".join(ids)})
        r.raise_for_status()
        for block in r.json().get("data") or []:
            market = market_map.get(block.get("marketTypeId"))
            for event_id, m in (block.get("eventMarkets") or {}).items():
                ev = events.get(event_id)
                if not ev or not market or m.get("marketStatus") != "ACTIVE":
                    continue
                if market == "OU":
                    if m.get("specifiers") not in OU_LINES:
                        continue
                    market_key = f"OU_{m['specifiers']}"
                else:
                    market_key = market
                for o in m.get("outcomes") or []:
                    oc = OUTCOMES.get(o.get("outcomeTypeId"))
                    if oc and (market_key != "12" and market_key != "12_OT" or oc in ("1", "2")):
                        ev.add(market_key, oc, o.get("odds"))

    async def close(self) -> None:
        await self.client.aclose()
