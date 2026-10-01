"""Mozzart: plain HTTP requests get 429 + Cloudflare challenge, so we keep a
headless browser open on mozzartbet.com and call their API with fetch() from
inside the page (it inherits the cookies/fingerprint the site expects).

Uses the Microsoft Edge that ships with Windows (channel="msedge"), so no
extra browser download is needed. Set MOZZART_BROWSER=chromium to use
Playwright's bundled Chromium instead (`playwright install chromium`)."""
from __future__ import annotations

import os

from arb.models import Event, utc_from_ms
from arb.scrapers.base import Scraper

HOME_URL = "https://www.mozzartbet.com/sr/kladjenje/sport/1"
PAGE_SIZE = 50
MAX_PAGES = 40

# Runs inside the page. Returns a compact list so we don't ship ~7KB per match to Python.
FETCH_JS = """
async ({sportId, pageSize, maxPages}) => {
  const out = [];
  for (let pg = 0; pg < maxPages; pg++) {
    const r = await fetch('/betting/matches', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*',
                'medium': 'PREMATCH_WEB'},
      body: JSON.stringify({date: 'all_days', sort: 'bycompetition', currentPage: pg, pageSize,
                            sportId, competitionIds: [], search: '', matchTypeId: 0}),
    });
    if (!r.ok) return {error: r.status, items: out};
    const items = (await r.json()).items || [];
    for (const m of items) {
      const odds = [];
      for (const g of m.oddsGroup || []) for (const o of g.odds || []) {
        if (o.oddStatus !== 'ACTIVE') continue;
        odds.push([o.game && o.game.id, o.subgame && o.subgame.name, o.specialOddValue || '', o.value]);
      }
      out.push({id: m.id, start: m.startTime, home: m.home && m.home.name, away: m.visitor && m.visitor.name,
                league: m.competition && m.competition.name, status: m.status && m.status.id,
                br: m.originInfo && m.originInfo.lbMatchId, odds});
    }
    if (items.length < pageSize) break;
    await new Promise(res => setTimeout(res, 300));
  }
  return {items: out};
}
"""

# Mozzart game ids differ per sport: sportId -> (our sport, {game id: market})
SPORT_GAMES = {
    1: ("football", {2: "1X2", 62: "BTTS", 6: "DC"}),
    2: ("basketball", {10: "12_OT", 6: "1X2"}),  # "Pobednik meča sa ev. produžecima" / regular 1X2
    5: ("tennis", {4: "12"}),
    4: ("hockey", {1: "1X2", 2: "DC"}),
    7: ("handball", {1: "1X2"}),
    9: ("table_tennis", {3: "12"}),
}
GAME_FOOTBALL_TOTAL = 30
TOTALS = {
    "0-1": ("OU_1.5", "U"), "2+": ("OU_1.5", "O"),
    "0-2": ("OU_2.5", "U"), "3+": ("OU_2.5", "O"),
    "0-3": ("OU_3.5", "U"), "4+": ("OU_3.5", "O"),
}


class MozzartScraper(Scraper):
    name = "Mozzart"

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None

    async def _ensure_page(self):
        if self._page and not self._page.is_closed():
            return self._page
        from playwright.async_api import async_playwright

        if not self._pw:
            self._pw = await async_playwright().start()
        if not self._browser or not self._browser.is_connected():
            channel = os.getenv("MOZZART_BROWSER", "msedge")
            # Mozzart answers 429 to anything that looks automated: hide navigator.webdriver ...
            kwargs = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if channel != "chromium":
                kwargs["channel"] = channel
            self._browser = await self._pw.chromium.launch(**kwargs)
        # ... and the "HeadlessChrome" token in the user agent.
        ver = self._browser.version
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
        if os.getenv("MOZZART_BROWSER", "msedge") == "msedge":
            ua += f" Edg/{ver}"
        ctx = await self._browser.new_context(
            locale="sr-RS", user_agent=ua, viewport={"width": 1366, "height": 800}
        )
        self._page = await ctx.new_page()
        await self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        await self._page.wait_for_timeout(3000)
        return self._page

    async def _reset_page(self) -> None:
        if self._page:
            try:
                await self._page.context.close()
            except Exception:
                pass
        self._page = None

    async def fetch(self) -> list[Event]:
        page = await self._ensure_page()
        events: list[Event] = []
        # one sport after another: parallel calls from the same page trip their rate limit
        for sport_id in SPORT_GAMES:
            try:
                res = await page.evaluate(
                    FETCH_JS, {"sportId": sport_id, "pageSize": PAGE_SIZE, "maxPages": MAX_PAGES}
                )
            except Exception:
                await self._reset_page()
                raise
            events += [ev for m in res["items"] if (ev := self._parse(m, sport_id))]
            if res.get("error"):
                # Probably rate-limited / challenged: start a fresh session next time.
                await self._reset_page()
                if not events:
                    raise RuntimeError(f"Mozzart: HTTP {res['error']}")
                break
        return events

    def _parse(self, m: dict, sport_id: int) -> Event | None:
        if not m.get("home") or not m.get("away") or m.get("status") not in (0, None):
            return None
        sport, games = SPORT_GAMES[sport_id]
        ev = Event(
            bookie=self.name,
            event_id=str(m["id"]),
            sport=sport,
            home=m["home"],
            away=m["away"],
            start=utc_from_ms(m["start"]),
            league=m.get("league") or "",
            betradar_id=str(m["br"]) if m.get("br") else None,
            url=f"https://www.mozzartbet.com/sr/kladjenje/sport/{sport_id}/match/{m['id']}",
        )
        for game_id, sub, special, value in m["odds"]:
            sub = (sub or "").strip()
            market = games.get(game_id)
            if market == "1X2" and sub in ("1", "X", "2"):
                ev.add("1X2", sub, value)
            elif market in ("12", "12_OT") and sub in ("1", "2"):
                ev.add(market, sub, value)
            elif market == "DC" and sub in ("1X", "12", "X2"):
                ev.add("DC", sub, value)
            elif market == "BTTS" and sub in ("GG", "NG"):
                ev.add("BTTS", sub, value)
            elif sport == "football" and game_id == GAME_FOOTBALL_TOTAL and sub in TOTALS:
                ev.add(*TOTALS[sub], value)
        return ev if ev.markets else None

    async def close(self) -> None:
        await self._reset_page()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
