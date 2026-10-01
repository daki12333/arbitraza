"""Shuffle (crypto casino, in-house sportsbook on the Betradar feed, own margins).
GraphQL, plain HTTP. The server only accepts the queries its site sends (other
query texts fail validation), so LEAGUES_QUERY / COMPETITION_QUERY are verbatim;
both return plain JSON. One call lists a sport's competitions, then one call per
competition returns up to 10 upcoming matches with the main market.
Serbian ISPs block shuffle.com in DNS -> Cloudflare DNS-over-HTTPS (wild.resolve)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from arb.models import Event
from arb.scrapers.base import Scraper, make_client
from arb.scrapers.wild import resolve

HOST = "shuffle.com"
PATH = "/main-api/graphql/sports/graphql-sports"
REFRESH_SECONDS = 300  # ~250 calls per crawl and the API rate-limits: keep it slow
PARALLEL = 2
DELAY = 0.4  # seconds between calls per worker
# shuffle sport -> (our sport, url slug, main market to ask for)
SPORTS = {
    "SOCCER": ("football", "soccer", "1_BETRADAR"),
    "BASKETBALL": ("basketball", "basketball", "219_BETRADAR"),
    "TENNIS": ("tennis", "tennis", "186_BETRADAR"),
    "ICE_HOCKEY": ("hockey", "ice-hockey", "1_BETRADAR"),
    "HANDBALL": ("handball", "handball", "1_BETRADAR"),
    "VOLLEYBALL": ("volleyball", "volleyball", "186_BETRADAR"),
    "TABLE_TENNIS": ("table_tennis", "table-tennis", "186_BETRADAR"),
}

LEAGUES_QUERY = ('query GetAllSportsLeagues($sports: Sports!, $language: Language) {\n'
                 '  sportsCategories: sportsCategoriesV2(sports: $sports, language: $language)\n}')
COMPETITION_QUERY = (
    'query GetSportsCompetition($slug: String, $id: String, $language: Language, $prioritizedMarketTypeId: String, '
    '$searchType: SportsSearchType!, $sportsGroupRulesId: String, $fixtureFirst: Int, $tournamentEndTime: DateTime) {\n'
    '  sportsCompetition: sportsCompetitionV2(\n    slug: $slug\n    id: $id\n    language: $language\n'
    '    prioritizedMarketTypeId: $prioritizedMarketTypeId\n    searchType: $searchType\n'
    '    sportsGroupRulesId: $sportsGroupRulesId\n    fixtureFirst: $fixtureFirst\n'
    '    tournamentEndTime: $tournamentEndTime\n  )\n}')


class ShuffleScraper(Scraper):
    name = "Shuffle"
    region = "crypto"
    fetch_timeout = 300  # rate-limited API: a full crawl takes ~2 min

    def __init__(self) -> None:
        self.client = make_client({
            "content-type": "application/json",
            "accept": "application/graphql-response+json,application/json;q=0.9",
            "referer": "https://shuffle.com/sports",
        }, timeout=30)
        self._ip: str | None = None
        self._resolved = False
        self._sem = asyncio.Semaphore(PARALLEL)
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def _gql(self, op: str, query: str, variables: dict) -> dict:
        if not self._resolved:
            self._ip, self._resolved = await resolve(HOST), True
        body = {"operationName": op, "variables": variables, "query": query}
        for attempt in range(3):
            async with self._sem:
                if self._ip:
                    r = await self.client.post(f"https://{self._ip}{PATH}", json=body,
                                               headers={"Host": HOST}, extensions={"sni_hostname": HOST})
                else:
                    r = await self.client.post(f"https://{HOST}{PATH}", json=body)
                await asyncio.sleep(DELAY)
            if r.status_code in (403, 502, 503):
                self._resolved = False
            r.raise_for_status()
            d = r.json()
            if d.get("errors") and not d.get("data"):
                msg = d["errors"][0].get("message", "")
                if "TOO_MANY" in msg and attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))  # rate limited: back off and retry
                    continue
                raise RuntimeError(f"Shuffle: {msg[:100]}")
            return d["data"]
        raise RuntimeError("Shuffle: rate limited")

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        results = []
        for s in SPORTS:  # one sport at a time (rate limit)
            try:
                results.append(await self._sport(s))
            except Exception as e:
                results.append(e)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events:
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]
        self._cache, self._cache_at = events, time.time()
        return events

    async def _sport(self, key: str) -> list[Event]:
        cats = (await self._gql("GetAllSportsLeagues", LEAGUES_QUERY, {"sports": key, "language": "en"}))["sportsCategories"]
        comps = [(cat, comp) for cat in cats or [] for comp in cat.get("competitions") or [] if comp.get("fixturesCount")]
        results = await asyncio.gather(*(self._competition(key, cat, comp) for cat, comp in comps), return_exceptions=True)
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _competition(self, key: str, cat: dict, comp: dict) -> list[Event]:
        sport, slug, market = SPORTS[key]
        d = await self._gql("GetSportsCompetition", COMPETITION_QUERY, {
            "slug": comp["slug"], "language": "en", "prioritizedMarketTypeId": market,
            "searchType": "SUB_FEATURED", "fixtureFirst": 10})
        c = d.get("sportsCompetition") or {}
        out = []
        for f in (c.get("fixtures") or {}).get("nodes") or []:
            ev = self._parse(f, sport, slug, cat, comp)
            if ev:
                out.append(ev)
        return out

    def _parse(self, f: dict, sport: str, slug: str, cat: dict, comp: dict) -> Event | None:
        if f.get("status") != "PREMATCH" or f.get("isOutrightLike"):
            return None
        teams = f.get("competitors") or []
        home = next((t["displayName"] for t in teams if t.get("isHome")), None)
        away = next((t["displayName"] for t in teams if not t.get("isHome")), None)
        if not home or not away or not f.get("startTime"):
            return None
        start = datetime.fromisoformat(f["startTime"].replace("Z", "+00:00"))
        if start <= datetime.now(timezone.utc):
            return None
        br = ((f.get("preMatchWidget") or {}).get("matchId") or "").rsplit(":", 1)[-1]
        ev = Event(
            bookie=self.name,
            event_id=f["id"],
            sport=sport,
            home=home,
            away=away,
            start=start,
            league=f"{cat.get('name', '')} {comp.get('name', '')}".strip(),
            betradar_id=br if br.isdigit() else None,
            url=f"https://shuffle.com/sports/{slug}/{cat.get('slug')}/{comp.get('slug')}/{f.get('slug')}",
        )
        default = ((f.get("defaultMarketsInfo") or {}).get("defaultMarket") or {}).get("odds") or []
        for m in default:
            if m.get("status") != "OPEN" or m.get("inPlay"):
                continue
            for s in m.get("selections") or []:
                if s.get("status") != "TRADING":
                    continue
                try:
                    _, market_id, outcome = s["providerId"].rsplit("/", 2)
                    odd = 1 + int(s["oddsNumerator"]) / int(s["oddsDenominator"])
                except (KeyError, ValueError, ZeroDivisionError):
                    continue
                if market_id == "1" and sport in ("football", "hockey", "handball"):
                    ours = {"1": "1", "2": "X", "3": "2"}.get(outcome)
                    if ours:
                        ev.add("1X2", ours, round(odd, 3))
                elif (market_id, sport) in (("219", "basketball"), ("186", "tennis"), ("186", "volleyball"),
                                            ("186", "table_tennis")) and outcome in ("4", "5"):
                    ev.add("12_OT" if sport == "basketball" else "12", "1" if outcome == "4" else "2", round(odd, 3))
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()
