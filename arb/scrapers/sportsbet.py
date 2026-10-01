"""Sportsbet.io (crypto sportsbook). GraphQL behind Cloudflare, so like Stake we keep a
headless Edge on sportsbet.io and query from inside the page. Their server only
accepts the exact queries the site itself sends (custom queries get HTTP 500), so
LIST_QUERY / EVENTS_QUERY below are copied verbatim from the site.

Flow per sport: one list call -> all tournaments, then one call per tournament
(~130 for football). Only each match's main market (1X2 / winner) is listed.
It is heavy, so a full crawl runs at most every REFRESH_SECONDS."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from arb.models import Event
from arb.scrapers.base import Scraper

HOME_URL = "https://sportsbet.io/sports/soccer"
REFRESH_SECONDS = 120
PARALLEL = 6
# sportsbet sport slug -> our sport
SPORTS = {"soccer": "football", "basketball": "basketball", "tennis": "tennis", "ice-hockey": "hockey",
          "handball": "handball", "volleyball": "volleyball", "table-tennis": "table_tennis"}

LIST_QUERY = 'query EventBoardListQuery($language: String!, $slug: String!, $timePeriod: SportsbetNewGraphqlSportLeagues!, $leagueTournaments: SportsbetNewGraphqlLeagueTournaments!, $featuredLeagueTournaments: SportsbetNewGraphqlFeaturedLeagueTournaments!, $tournamentEventCount: SportsbetNewGraphqlTournamentEventCount!, $site: String) {\n  sportsbetNewGraphql {\n    id\n    getSportBySlug(slug: $slug, site: $site) {\n      id\n      slug\n      viewType\n      name(language: $language)\n      featuredLeague {\n        id\n        name(language: $language)\n        iconCode {\n          alpha2\n          __typename\n        }\n        tournaments(childType: $featuredLeagueTournaments) {\n          id\n          slug\n          name(language: $language)\n          eventCount(childType: $tournamentEventCount)\n          league {\n            id\n            slug\n            name(language: $language)\n            enName: name(language: "en")\n            __typename\n          }\n          __typename\n        }\n        __typename\n      }\n      leagues(childType: $timePeriod) {\n        id\n        name(language: $language)\n        enName: name(language: "en")\n        slug\n        iconCode {\n          alpha2\n          __typename\n        }\n        tournaments(childType: $leagueTournaments) {\n          id\n          slug\n          name(language: $language)\n          eventCount(childType: $tournamentEventCount)\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n'

EVENTS_QUERY = 'query EventBoardTournamentEventsQuery($language: String!, $tournamentId: GraphqlId!, $childType: SportsbetNewGraphqlTournamentEvents!, $cricketIncluded: Boolean!) {\n  sportsbetNewGraphql {\n    id\n    getTournamentById(id: $tournamentId) {\n      id\n      events(childType: $childType) {\n        ...EventBoardListEventFragment\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n\nfragment EventBoardListEventFragment on SportsbetNewGraphqlEvent {\n  id\n  __typename\n  type\n  status\n  start_time\n  market_count\n  live_odds\n  slug\n  name(language: $language)\n  enName: name(language: "en")\n  maxBetAvailable\n  league {\n    id\n    __typename\n    slug\n    betBoostMultiplier\n  }\n  tournament {\n    id\n    __typename\n    slug\n    betBoostMultiplier\n  }\n  hasPremiumCricketScoringData\n  premiumCricketScoringData @include(if: $cricketIncluded) {\n    id\n    battingTeam {\n      id\n      teamName\n      teamRuns\n      teamWickets\n      teamOvers\n      __typename\n    }\n    previousInnings {\n      id\n      teamName\n      runs\n      wickets\n      __typename\n    }\n    __typename\n  }\n  videoStream {\n    id\n    __typename\n    streamAvailable\n  }\n  sport {\n    id\n    __typename\n    slug\n    viewType\n    iconCode\n    betBoostMultiplier\n  }\n  competitors {\n    id\n    __typename\n    name(language: $language)\n    enName\n    type\n  }\n  information {\n    id\n    __typename\n    match_time\n    match_status_translations(language: $language)\n    period_scores {\n      id\n      __typename\n      home_score\n      away_score\n    }\n    home_score\n    away_score\n    home_gamescore\n    away_gamescore\n    provider_product_id\n    extraData {\n      id\n      server\n      __typename\n    }\n  }\n  isSportcastFixtureActive\n  sportcastFixtureId\n  getFirstActiveListMarket {\n    ...EventBoardListMarketFragment\n    __typename\n  }\n  asian {\n    id\n    __typename\n    ftMatchWinner {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n    ftTotal {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n    ftHandicap {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n    htMatchWinner {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n    htTotal {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n    htHandicap {\n      ...EventBoardListMarketFragment\n      __typename\n    }\n  }\n}\n\nfragment EventBoardListMarketFragment on SportsbetNewGraphqlMarket {\n  id\n  __typename\n  status\n  specifiers\n  enName: name(language: "en")\n  name(language: $language)\n  selections {\n    id\n    __typename\n    enName: name(language: "en")\n    name(language: $language)\n    active\n    odds\n    providerProductId\n    competitorType\n  }\n  market_type {\n    id\n    __typename\n    translation_key\n    type\n    settings {\n      id\n      betBoostMultiplier\n      __typename\n    }\n  }\n}\n'

FETCH_JS = """
async ({listQuery, eventsQuery, slug, parallel}) => {
  const gql = async (operationName, query, variables) => {
    const r = await fetch('/graphql', {method: 'POST', headers: {'content-type': 'application/json'},
                                       body: JSON.stringify({operationName, variables, query})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return (await r.json()).data.sportsbetNewGraphql;
  };
  const sport = (await gql('EventBoardListQuery', listQuery, {
    language: 'en', slug, timePeriod: 'ALL', leagueTournaments: 'ALL', featuredLeagueTournaments: 'ALL',
    tournamentEventCount: 'ALL', site: 'sportsbet'})).getSportBySlug;
  const tours = [];
  for (const l of (sport && sport.leagues) || []) for (const t of l.tournaments || [])
    if (t.eventCount) tours.push([l.slug, l.name, t.id, t.slug, t.name]);
  const out = [];
  let errors = 0;
  const work = async () => {
    while (tours.length) {
      const [lslug, lname, tid, tslug, tname] = tours.shift();
      let evs;
      try {
        evs = (await gql('EventBoardTournamentEventsQuery', eventsQuery,
               {language: 'en', tournamentId: tid, childType: 'ALL', cricketIncluded: false})).getTournamentById.events;
      } catch (e) { errors++; continue; }
      for (const e of evs || []) {
        if (e.status !== '1') continue;  // 1 = not started, 2 = live
        const m = e.getFirstActiveListMarket;
        const home = (e.competitors || []).find(c => c.type === 'home');
        const away = (e.competitors || []).find(c => c.type === 'away');
        if (!m || !home || !away) continue;
        out.push({slug: e.slug, start: e.start_time, home: home.name, away: away.name,
                  league: lname + ' ' + tname, lslug, tslug, market: m.enName,
                  sels: (m.selections || []).map(s => [s.competitorType, s.active ? s.odds : 0])});
      }
    }
  };
  await Promise.all(Array.from({length: parallel}, work));
  return {items: out, errors};
}
"""


class SportsbetScraper(Scraper):
    name = "Sportsbet.io"
    region = "crypto"

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def _ensure_page(self):
        if self._page and not self._page.is_closed():
            return self._page
        from playwright.async_api import async_playwright

        if not self._pw:
            self._pw = await async_playwright().start()
        if not self._browser or not self._browser.is_connected():
            channel = os.getenv("MOZZART_BROWSER", "msedge")
            kwargs = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if channel != "chromium":
                kwargs["channel"] = channel
            self._browser = await self._pw.chromium.launch(**kwargs)
        ver = self._browser.version
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
        if os.getenv("MOZZART_BROWSER", "msedge") == "msedge":
            ua += f" Edg/{ver}"
        ctx = await self._browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 800})
        self._page = await ctx.new_page()
        await self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        await self._page.wait_for_timeout(5000)
        return self._page

    async def _reset_page(self) -> None:
        if self._page:
            try:
                await self._page.context.close()
            except Exception:
                pass
        self._page = None

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        page = await self._ensure_page()
        events: list[Event] = []
        for slug, sport in SPORTS.items():
            try:
                res = await page.evaluate(FETCH_JS, {"listQuery": LIST_QUERY, "eventsQuery": EVENTS_QUERY,
                                                     "slug": slug, "parallel": PARALLEL})
            except Exception:
                await self._reset_page()
                if events:
                    break
                raise
            events += [ev for m in res["items"] if (ev := self._parse(m, slug, sport))]
        self._cache, self._cache_at = events, time.time()
        return events

    def _parse(self, m: dict, slug: str, sport: str) -> Event | None:
        start = datetime.fromtimestamp(m["start"], tz=timezone.utc)
        if start <= datetime.now(timezone.utc):
            return None
        ev = Event(
            bookie=self.name,
            event_id=m["slug"],
            sport=sport,
            home=m["home"],
            away=m["away"],
            start=start,
            league=m.get("league") or "",
            url=f"https://sportsbet.io/sports/{slug}/{m['lslug']}/{m['tslug']}/matches?event={m['slug']}",
        )
        odds = {side: o for side, o in m["sels"]}
        name = (m.get("market") or "").lower()
        if name == "1x2" and sport in ("football", "hockey", "handball"):
            for side, oc in (("HOME", "1"), ("DRAW", "X"), ("AWAY", "2")):
                ev.add("1X2", oc, odds.get(side))
        elif len(odds) == 2 and "DRAW" not in odds:
            if sport == "basketball" and "overtime" in name:
                market = "12_OT"
            elif sport in ("tennis", "volleyball", "table_tennis") and name in ("winner", "match winner"):
                market = "12"
            else:
                return None
            ev.add(market, "1", odds.get("HOME"))
            ev.add(market, "2", odds.get("AWAY"))
        return ev if ev.markets else None

    async def close(self) -> None:
        await self._reset_page()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
