"""Duelbits (crypto casino with its own sportsbook on the Betradar feed, own margins).
Their JSON API sits behind Cloudflare, so it's called from a tab of the shared
browser. homepage/events returns every upcoming match, 5 countries per call, with
its main market; event ids are Betradar ids ("sr:match:N")."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from arb.models import Event
from arb.scrapers.base import Scraper, slugify
from arb.scrapers.browser import BrowserPage

REFRESH_SECONDS = 120
# betradar sport id -> (our sport, duelbits url slug)
SPORTS = {1: ("football", "soccer"), 2: ("basketball", "basketball"), 5: ("tennis", "tennis"),
          4: ("hockey", "ice-hockey"), 6: ("handball", "handball"), 23: ("volleyball", "volleyball"),
          20: ("table_tennis", "table-tennis")}

FETCH_JS = """
async ({sportId}) => {
  const out = [];
  let offset = 0;
  for (let i = 0; i < 60 && offset !== null && offset !== undefined; i++) {
    const q = new URLSearchParams({sportId, time: 'all', limit: '5', offset: String(offset),
                                   timezone: 'Europe/Budapest', lang: 'en', state: 'home'});
    const r = await fetch('/ws/betradar/homepage/events?' + q, {headers: {'authorization': 'Bearer null',
                                                                      'content-type': 'application/json'}});
    if (!r.ok) return {error: r.status, items: out};
    const d = await r.json();
    for (const cat of d.data || []) for (const t of cat.tournaments || []) for (const e of t.events || []) {
      if (e.status !== 'not_started' || e.isHidden) continue;
      const home = (e.competitors || []).find(c => c.qualifier === 'home');
      const away = (e.competitors || []).find(c => c.qualifier === 'away');
      if (!home || !away) continue;
      const markets = (e.topMarket || []).filter(m => m && m.status === 'active').map(m => [
        String(m.marketId), (m.outcomes || []).filter(o => o && o.active).map(o => [String(o.extId), o.odds])]);
      out.push({id: e.id, start: e.scheduled || e.startTime, home: home.name, away: away.name,
                league: (cat.categoryName || '') + ' ' + (t.tournamentName || ''), markets});
    }
    offset = d.nextOffset;
  }
  return {items: out};
}
"""


class DuelbitsScraper(Scraper, BrowserPage):
    name = "Duelbits"
    region = "crypto"
    home_url = "https://duelbits.com/en/sportsbook/home/sports/soccer"

    def __init__(self) -> None:
        BrowserPage.__init__(self)
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        page = await self.page()
        events: list[Event] = []
        for sid, (sport, slug) in SPORTS.items():
            try:
                res = await page.evaluate(FETCH_JS, {"sportId": f"sr:sport:{sid}"})
            except Exception:
                if page.is_closed() or not events:
                    await self.reset()
                    raise
                continue  # one sport's odd data must not cost the others
            events += [ev for m in res["items"] if (ev := self._parse(m, sport, slug))]
            if res.get("error"):
                await self.reset()
                if not events:
                    raise RuntimeError(f"Duelbits: HTTP {res['error']}")
                break
        self._cache, self._cache_at = events, time.time()
        return events

    def _parse(self, m: dict, sport: str, slug: str) -> Event | None:
        start = datetime.fromisoformat(m["start"].replace("Z", "+00:00"))
        if start <= datetime.now(timezone.utc):
            return None
        br = m["id"].rsplit(":", 1)[-1]
        ev = Event(
            bookie=self.name,
            event_id=m["id"],
            sport=sport,
            home=m["home"],
            away=m["away"],
            start=start,
            league=m["league"].strip(),
            betradar_id=br if br.isdigit() else None,
            url=(f"https://duelbits.com/en/sportsbook/{slug}/match/{br}-"
                 f"{slugify(m['home']).title()}-vs-{slugify(m['away']).title()}"),
        )
        for market_id, outcomes in m["markets"]:
            odds = dict(outcomes)
            if market_id == "1" and sport in ("football", "hockey", "handball"):
                for oc, ours in (("1", "1"), ("2", "X"), ("3", "2")):
                    ev.add("1X2", ours, odds.get(oc))
            elif market_id in ("186", "219", "340") and set(odds) >= {"4", "5"}:
                if sport == "basketball" and market_id == "219":
                    market = "12_OT"
                elif sport in ("tennis", "volleyball", "table_tennis") and market_id == "186":
                    market = "12"
                else:
                    continue
                ev.add(market, "1", odds["4"])
                ev.add(market, "2", odds["5"])
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.reset()
