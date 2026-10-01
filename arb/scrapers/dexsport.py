"""Dexsport.io (crypto sportsbook). Odds only come over their websocket
(prod.dexsport.work/ws), whose URL carries a short-lived token, so we keep a
headless Edge on dexsport.io, take the socket URL the page opens and run our own
socket from inside the page:
join discipline -> tournaments -> events -> the events' main markets.

Line "1" is the prematch line. Market types: 2 = match winner (1/X/2),
1 = 2-way winner, 7 = double chance, 4 = total; interval 1000 = full time,
1001 = incl. overtime, 1030 = whole match (tennis/volleyball)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from arb.models import Event, clean_line, line_str
from arb.scrapers.base import Scraper

HOME_URL = "https://dexsport.io/sports/football/"
REFRESH_SECONDS = 120
TOKEN_SECONDS = 300  # reload the page (fresh socket token) when the URL is older than this
# dexsport discipline -> our sport
SPORTS = {"football": "football", "basketball": "basketball", "tennis": "tennis", "hockey": "hockey",
          "handball": "handball", "volleyball": "volleyball"}
OU_LINES = {"1.5", "2.5", "3.5"}

FETCH_JS = """
async ({url, disciplines}) => {
  const ws = new WebSocket(url);
  const store = {discipline: {}, tournament: {}, event: {}, market: {}};
  let last = Date.now();
  ws.onmessage = (m) => {
    let d; try { d = JSON.parse(m.data); } catch (e) { return; }
    if (d[0] !== 'batch') return;
    for (const [kind, lid, ver, obj] of d[1]) if (store[kind]) {
      store[kind][lid] = Object.assign(store[kind][lid] || {}, obj);
      last = Date.now();
    }
  };
  await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = () => bad(new Error('socket failed')); });
  const send = (x) => ws.send(JSON.stringify(x));
  // wait until nothing new has arrived for `quiet` ms (max `limit` ms)
  const settle = async (quiet, limit) => {
    const t0 = Date.now(); last = Date.now();
    while (Date.now() - last < quiet && Date.now() - t0 < limit) await new Promise(r => setTimeout(r, 200));
  };
  const joinAll = async (kind, ids, chunk) => {
    for (let i = 0; i < ids.length; i += chunk) send(['join', kind, ids.slice(i, i + chunk)]);
    await settle(1500, 20000);
  };
  await joinAll('discipline', disciplines, 50);
  await joinAll('tournament', Object.values(store.discipline).flatMap(d => d.tournamentIds || []), 200);
  const now = Date.now() / 1000;
  const eids = Object.values(store.tournament).flatMap(t =>
    (t.eventRefs || []).filter(e => e.type === 0 && e.startTime > now).map(e => e.lid));
  await joinAll('event', eids, 200);
  await joinAll('market', Object.values(store.event).flatMap(e => (e.mainMarketIds || []).filter(Boolean)), 300);
  ws.close();
  const tours = store.tournament, markets = store.market;
  const out = [];
  for (const e of Object.values(store.event)) {
    if (e.type !== 0 || !e.competitors || e.competitors.length !== 2) continue;
    const t = tours[e.pid] || {};
    const mk = (e.mainMarketIds || []).filter(Boolean).map(id => markets[id]).filter(Boolean).map(m => [
      m.marketTypeId, m.intervalId, m.param,
      (m.outcomes || []).filter(o => !o.isFrozen && o.status === 3).map(o => [o.shortName, o.price])]);
    out.push({id: e.lid, start: e.startTime, sport: e.did, slug: e.slug, status: e.status,
              home: e.competitors[0].name, away: e.competitors[1].name,
              league: ((t.region && t.region.name) || '') + ' ' + (t.name || ''), markets: mk});
  }
  return out;
}
"""


class DexsportScraper(Scraper):
    name = "Dexsport"
    region = "crypto"

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._ws_url: str | None = None
        self._ws_at = 0.0
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def _ws(self) -> tuple:
        """The page and a fresh socket URL (the token in it expires after ~10 min)."""
        if self._page and not self._page.is_closed() and self._ws_url and time.time() - self._ws_at < TOKEN_SECONDS:
            return self._page, self._ws_url
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
        if not self._page or self._page.is_closed():
            ver = self._browser.version
            ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
            if os.getenv("MOZZART_BROWSER", "msedge") == "msedge":
                ua += f" Edg/{ver}"
            ctx = await self._browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 800})
            self._page = await ctx.new_page()
        urls: list[str] = []
        handler = lambda ws: urls.append(ws.url) if "dexsport.work/ws" in ws.url else None
        self._page.on("websocket", handler)
        try:
            await self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(40):
                if urls:
                    break
                await self._page.wait_for_timeout(500)
        finally:
            self._page.remove_listener("websocket", handler)
        if not urls:
            raise RuntimeError("Dexsport: socket se nije otvorio")
        self._ws_url, self._ws_at = urls[-1], time.time()
        return self._page, self._ws_url

    async def _reset_page(self) -> None:
        if self._page:
            try:
                await self._page.context.close()
            except Exception:
                pass
        self._page, self._ws_url = None, None

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        page, url = await self._ws()
        try:
            items = await page.evaluate(FETCH_JS, {"url": url, "disciplines": [f"1.{d}" for d in SPORTS]})
        except Exception:
            await self._reset_page()
            raise
        now = datetime.now(timezone.utc)
        events = [ev for m in items if (ev := self._parse(m, now))]
        self._cache, self._cache_at = events, time.time()
        return events

    def _parse(self, m: dict, now: datetime) -> Event | None:
        sport = SPORTS.get(m.get("sport"))
        start = datetime.fromtimestamp(m["start"], tz=timezone.utc)
        if not sport or start <= now:
            return None
        ev = Event(
            bookie=self.name,
            event_id=m["id"],
            sport=sport,
            home=m["home"],
            away=m["away"],
            start=start,
            league=(m.get("league") or "").strip(),
            url=f"https://dexsport.io/sports/{m['slug']}/bets/",  # slug = football/home-vs-away-<id>
        )
        for mtype, interval, param, outcomes in m["markets"]:
            odds = {k: v for k, v in outcomes if k}  # some outcomes come without a name
            if interval == 1002 and sport == "football":  # first half
                if mtype == 2 and set(odds) == {"1", "X", "2"}:
                    for oc in ("1", "X", "2"):
                        ev.add("H1_1X2", oc, odds[oc])
                elif mtype in (4, 589) and param is not None and clean_line(param):
                    ev.add(f"H1_OU_{line_str(param)}", "O", next((v for k, v in odds.items() if k.startswith("O ")), None))
                    ev.add(f"H1_OU_{line_str(param)}", "U", next((v for k, v in odds.items() if k.startswith("U ")), None))
                elif mtype in (3, 588) and param is not None and clean_line(param):
                    ev.add(f"H1_AH_{line_str(param)}", "1", next((v for k, v in odds.items() if k.startswith("1 ")), None))
                    ev.add(f"H1_AH_{line_str(param)}", "2", next((v for k, v in odds.items() if k.startswith("2 ")), None))
                continue
            if mtype in (1, 2) and interval == 1000 and set(odds) == {"1", "X", "2"}:
                if sport in ("football", "hockey", "handball", "basketball"):
                    for oc in ("1", "X", "2"):
                        ev.add("1X2", oc, odds[oc])
            elif mtype in (1, 2) and set(odds) == {"1", "2"}:
                if sport == "basketball" and interval == 1001:
                    ev.add("12_OT", "1", odds["1"])
                    ev.add("12_OT", "2", odds["2"])
                elif sport in ("tennis", "volleyball") and interval in (1000, 1030):
                    ev.add("12", "1", odds["1"])
                    ev.add("12", "2", odds["2"])
            elif mtype == 7 and interval == 1000 and sport in ("football", "hockey", "handball"):
                for oc in ("1X", "12", "X2"):
                    ev.add("DC", oc, odds.get(oc))
            elif mtype in (4, 589) and param is not None and (
                    (interval == 1000 and sport in ("football", "handball", "tennis"))
                    or (interval == 1001 and sport == "basketball")):  # basketball: incl. overtime
                over = next((v for k, v in odds.items() if k.startswith("O ")), None)
                under = next((v for k, v in odds.items() if k.startswith("U ")), None)
                ev.add_total(param, over, under)
            elif mtype in (3, 588) and param is not None and (
                    (interval == 1000 and sport in ("football", "handball", "tennis"))
                    or (interval == 1001 and sport == "basketball")):
                home = next((v for k, v in odds.items() if k.startswith("1 ")), None)
                away = next((v for k, v in odds.items() if k.startswith("2 ")), None)
                ev.add_handicap(param, home, away)  # param = the home side's line
        return ev if ev.markets else None

    async def close(self) -> None:
        await self._reset_page()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
