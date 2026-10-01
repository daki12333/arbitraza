"""CasinOK (crypto casino, Digitain sportsbook). Digitain's API answers plain HTTP
only sporadically, so like Stake we keep a headless Edge on CasinOK's sportsbook
page and call the API from inside it.

getheader = every prematch game (ids only) by sport/region/champ; then
getprematchgameall returns odds + team names for a batch of games. Selections
are identified by a global "pos" id (1/2/3 = 1X2, 57/58/59 = 1X/12/X2, ...).
Games carry "bid" = Betradar match id."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from arb.models import Event, clean_line, line_kind, line_str
from arb.scrapers.base import Scraper

BRAND = 302
HOME_URL = (f"https://casinok.com/sportsbookv4/index.html?brandid={BRAND}&brandname=casinok&lang=en"
            "&token=0&appv=&theme=default-dark-theme")
API = "https://analytics-sp.googleserv.tech/api"
REFRESH_SECONDS = 120
BATCH = 40
PARALLEL = 4

# digitain sport id -> our sport
SPORTS = {1: "football", 2: "basketball", 5: "tennis", 4: "hockey", 6: "handball", 22: "volleyball",
          19: "table_tennis"}
# selection "pos" -> (market, outcome); totals get their line from "h"
POS = {
    1: ("1X2", "1"), 2: ("1X2", "X"), 3: ("1X2", "2"),  # football
    57: ("DC", "1X"), 58: ("DC", "12"), 59: ("DC", "X2"),
    81: ("OU", "O"), 82: ("OU", "U"),  # football total goals (any line)
    318: ("H1_OU", "O"), 319: ("H1_OU", "U"),  # football 1st half total goals
    70: ("AH", "1"), 71: ("AH", "2"),  # football handicap
    715: ("AH", "1"), 716: ("AH", "2"), 717: ("OU", "O"), 718: ("OU", "U"),  # basketball incl. OT
    1057: ("AH", "1"), 1058: ("AH", "2"), 1077: ("OU", "O"), 1078: ("OU", "U"),  # tennis games
    697: ("12_OT", "1"), 698: ("12_OT", "2"),  # basketball, incl. overtime
    882: ("1X2", "1"), 883: ("1X2", "X"), 884: ("1X2", "2"),  # hockey
    1053: ("12", "1"), 1054: ("12", "2"),  # tennis
    1118: ("1X2", "1"), 1119: ("1X2", "X"), 1120: ("1X2", "2"),  # handball
    1844: ("12", "1"), 1845: ("12", "2"),  # volleyball
    1556: ("12", "1"), 1557: ("12", "2"),  # table tennis
}
OU_LINES = {1.5, 2.5, 3.5}

FETCH_JS = """
async ({api, brand, sports, positions, batch, parallel}) => {
  const get = async (u) => {
    const r = await fetch(u);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    let d = await r.json();
    return typeof d === 'string' ? JSON.parse(d) : d;
  };
  const header = (await get(api + '/sport/getheader/en')).EN.Sports;
  const info = {}, ids = [];
  for (const sid of sports) {
    const s = header[sid];
    if (!s) continue;
    for (const r of Object.values(s.Regions || {})) for (const c of Object.values(r.Champs || {}))
      for (const g of Object.values(c.GameSmallItems || {})) {
        info[g.ID] = [s.ID, r.Name + ' ' + c.Name];
        ids.push(g.ID);
      }
  }
  const pos = new Set(positions);
  const out = [];
  let errors = 0;
  const work = async () => {
    while (ids.length) {
      const chunk = ids.splice(0, batch);
      let d;
      try { d = await get(api + '/prematch/getprematchgameall/en/' + brand + '/?games=,' + chunk.join(',')); }
      catch (e) { errors++; continue; }
      const games = typeof d.game === 'string' ? JSON.parse(d.game) : (d.game || []);
      const teamsArr = typeof d.teams === 'string' ? JSON.parse(d.teams) : (d.teams || []);
      const teams = {};
      for (const t of teamsArr) teams[t.ID] = t.Name;
      for (const g of games) {
        if (g.s !== 0 || !teams[g.t1] || !teams[g.t2] || !info[g.id]) continue;
        const odds = [];
        for (const sels of Object.values(g.ev || {})) for (const o of Object.values(sels))
          if (pos.has(o.pos) && !o.lock) odds.push([o.pos, o.coef, o.h === undefined ? null : o.h, !!o.hism]);
        if (odds.length) out.push({id: g.id, home: teams[g.t1], away: teams[g.t2], start: g.stunix, bid: g.bid,
                                   sport: info[g.id][0], league: info[g.id][1], odds});
      }
    }
  };
  await Promise.all(Array.from({length: parallel}, work));
  return {items: out, errors};
}
"""


class CasinoKScraper(Scraper):
    name = "CasinOK"
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
        await self._page.wait_for_timeout(3000)
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
        try:
            res = await page.evaluate(FETCH_JS, {"api": API, "brand": BRAND, "sports": list(SPORTS),
                                                 "positions": list(POS), "batch": BATCH, "parallel": PARALLEL})
        except Exception:
            await self._reset_page()
            raise
        now = datetime.now(timezone.utc)
        events = [ev for g in res["items"] if (ev := self._parse(g, now))]
        self._cache, self._cache_at = events, time.time()
        return events

    def _parse(self, g: dict, now: datetime) -> Event | None:
        sport = SPORTS.get(g["sport"])
        start = datetime.fromtimestamp(g["start"], tz=timezone.utc)
        if not sport or start <= now:
            return None
        ev = Event(
            bookie=self.name,
            event_id=str(g["id"]),
            sport=sport,
            home=g["home"],
            away=g["away"],
            start=start,
            league=g.get("league") or "",
            betradar_id=str(g["bid"]) if g.get("bid") else None,
            url=f"https://casinok.com/sportsbook/prematch/match/{g['id']}",
        )
        for pos, coef, h, hism in g["odds"]:
            market, oc = POS[pos]
            if market in ("OU", "H1_OU"):
                if h is not None and clean_line(h) and line_kind(h) != "quarter":  # quarter lines: not verified here yet
                    ev.add(f"{market}_{line_str(h)}", oc, coef)
            elif market == "AH":
                if h is None:
                    continue
                # "h" is the home line; on the away side "hism" marks it that way, otherwise it's the away line
                home_line = float(h) if (oc == "1" or hism) else -float(h)
                if clean_line(home_line) and line_kind(home_line) != "quarter":
                    ev.add(f"AH_{line_str(home_line)}", oc, coef)
            else:
                ev.add(market, oc, coef)
        return ev if ev.markets else None

    async def close(self) -> None:
        await self._reset_page()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
