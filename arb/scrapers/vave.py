"""Vave (crypto casino with its own sportsbook platform, Betradar feed, own margins).
Public JSON API, plain HTTP: event/list pages through every upcoming match of a
sport with all its markets (vendorMarketId = Betradar market id). Pages are big,
so a full crawl runs at most every REFRESH_SECONDS."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

API = "https://platform.vave.com/api/event/list"
REFRESH_SECONDS = 300  # ~25 page calls per crawl and the API rate-limits (429)
PAGE = 100
# vave sport id -> (our sport, url alias)
SPORTS = {1: ("football", "football"), 2: ("basketball", "basketball"), 3: ("tennis", "tennis"),
          4: ("hockey", "ice-hockey"), 10: ("handball", "handball"), 7: ("volleyball", "volleyball"),
          15: ("table_tennis", "table-tennis"), 5: ("baseball", "baseball"),
          17: ("american_football", "american-football")}
# Betradar handicap / total market ids per sport (outcomes 1714/1715, 12/13)
HANDICAP = {"football": 16, "handball": 16, "basketball": 223, "tennis": 187, "baseball": 256,
            "american_football": 223}
TOTALS = {"football": 18, "handball": 18, "basketball": 225, "tennis": 189, "baseball": 258,
          "american_football": 225}


class VaveScraper(Scraper):
    name = "Vave"
    region = "crypto"

    def __init__(self) -> None:
        self.client = make_client(timeout=60)
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        sem = asyncio.Semaphore(3)

        async def one(sid):
            async with sem:
                return await self._sport(sid)

        results = await asyncio.gather(*(one(sid) for sid in SPORTS), return_exceptions=True)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events:
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]
        self._cache, self._cache_at = events, time.time()
        return events

    async def _page(self, sid: int, page: int) -> dict:
        params = [("lang", "en"), ("relations", "odds"), ("relations", "league"), ("relations", "competitors"),
                  ("relations", "sportCategories"), ("oddsExists_eq", 1), ("main", 1), ("period", 0),
                  ("sportId_eq", sid), ("limit", PAGE), ("status_in", 0), ("isLive", "false"), ("page", page)]
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        return r.json()["data"]

    async def _sport(self, sid: int) -> list[Event]:
        first = await self._page(sid, 1)
        pages = [first]
        for p in range(2, (first.get("lastPage") or 1) + 1):  # one page at a time: the API rate-limits
            pages.append(await self._page(sid, p))
        out = []
        for d in pages:
            rel = d.get("relations") or {}
            comps = {c["id"]: c["name"] for c in rel.get("competitors") or []}
            leagues = {l["id"]: l for l in rel.get("league") or []}
            cats = {c["id"]: c["name"] for c in rel.get("sportCategories") or []}
            odds = rel.get("odds") or {}
            for e in d.get("items") or []:
                ev = self._parse(e, sid, comps, leagues, cats, odds.get(str(e["id"])) or odds.get(e["id"]) or [])
                if ev:
                    out.append(ev)
        return out

    def _parse(self, e: dict, sid: int, comps: dict, leagues: dict, cats: dict, markets: list) -> Event | None:
        sport, alias = SPORTS[sid]
        home, away = comps.get(e.get("competitor1Id")), comps.get(e.get("competitor2Id"))
        if not home or not away or e.get("status") != 0 or not e.get("time"):
            return None
        if e.get("isHomeAwayReversed"):
            return None  # unclear which side the odds refer to - skip rather than risk a fake arb
        start = datetime.fromisoformat(e["time"]).replace(tzinfo=timezone.utc)
        if start <= datetime.now(timezone.utc):
            return None
        league = leagues.get(e.get("leagueId")) or {}
        br = (e.get("vendorEventId") or "").rsplit(":", 1)[-1]
        ev = Event(
            bookie=self.name,
            event_id=str(e["id"]),
            sport=sport,
            home=home,
            away=away,
            start=start,
            league=f"{cats.get(e.get('sportCategoryId'), '')} {league.get('name', '')}".strip(),
            betradar_id=br if br.isdigit() else None,
            url=(f"https://vave.com/prematch/{alias}/{e.get('leagueId')}-{league.get('defaultSlug', 'league')}/"
                 f"{e['id']}-{e.get('defaultSlug', '')}"),
        )
        for m in markets:
            if m.get("status") != 1:
                continue
            mid, spec = m.get("vendorMarketId"), m.get("specifiers") or ""
            o = {x.get("vendorOutcomeId"): x.get("odds") for x in m.get("outcomes") or [] if x.get("active") == 1}
            if mid == 1 and sport in ("football", "hockey", "handball"):
                for oc, ours in (("1", "1"), ("2", "X"), ("3", "2")):
                    ev.add("1X2", ours, o.get(oc))
            elif mid == 10 and sport in ("football", "hockey", "handball"):
                for oc, ours in (("9", "1X"), ("10", "12"), ("11", "X2")):
                    ev.add("DC", ours, o.get(oc))
            elif mid == 29 and sport == "football":
                ev.add("BTTS", "GG", o.get("74"))
                ev.add("BTTS", "NG", o.get("76"))
            elif mid == TOTALS.get(sport) and spec.startswith("total="):
                ev.add_total(spec[6:], o.get("12"), o.get("13"))
            elif mid == HANDICAP.get(sport) and spec.startswith("hcp=") and ":" not in spec:
                ev.add_handicap(spec[4:], o.get("1714"), o.get("1715"))
            elif mid == 251 and sport == "baseball":
                ev.add("12", "1", o.get("4"))
                ev.add("12", "2", o.get("5"))
            elif mid == 219 and sport in ("basketball", "american_football"):
                ev.add("12_OT", "1", o.get("4"))
                ev.add("12_OT", "2", o.get("5"))
            elif mid == 186 and sport in ("tennis", "volleyball", "table_tennis"):
                ev.add("12", "1", o.get("4"))
                ev.add("12", "2", o.get("5"))
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()
