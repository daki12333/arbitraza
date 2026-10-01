"""Stake.com (crypto sportsbook). The API is behind Cloudflare, so like Mozzart we keep
a headless Edge open on stake.com and POST to their GraphQL endpoint from inside
the page. Fixtures carry the Betradar id ("sr:match:N"), so they pair exactly.

Market templates are Betradar market ids: 1 = 1x2, 10 = double chance,
29 = both teams to score, 18 = total, 186 = winner, 219 = winner incl. overtime."""
from __future__ import annotations

import os
import re
from email.utils import parsedate_to_datetime

from arb.models import Event, clean_line, line_str
from arb.scrapers.base import Scraper

HOME_URL = "https://stake.com/sports/soccer"
PAGE_SIZE = 50
MAX_PAGES = 30

# stake sport slug -> (our sport, {template id: kind}); kinds: 1X2, DC, BTTS, WIN (2-way
# winner -> "12"), WIN_OT (-> "12_OT"), OU (total, any line), AH (2-way handicap, home line)
SPORTS = {
    "soccer": ("football", {"1": "1X2", "10": "DC", "29": "BTTS", "18": "OU", "16": "AH",
                            "60": "H1_1X2", "68": "H1_OU", "66": "H1_AH", "19": "T1_OU", "20": "T2_OU"}),
    "basketball": ("basketball", {"219": "WIN_OT", "1": "1X2", "223": "AH", "225": "OU"}),
    "tennis": ("tennis", {"186": "WIN", "187": "AH", "189": "OU"}),
    "ice-hockey": ("hockey", {"1": "1X2", "10": "DC"}),
    "handball": ("handball", {"1": "1X2", "10": "DC", "16": "AH", "18": "OU"}),
    "volleyball": ("volleyball", {"186": "WIN"}),
    "table-tennis": ("table_tennis", {"186": "WIN"}),
    "baseball": ("baseball", {"251": "WIN", "256": "AH", "258": "OU"}),
    "american-football": ("american_football", {"219": "WIN_OT", "223": "AH", "225": "OU"}),
    # esports (Oddin feed): 1 = match winner (two-way variant only), 2 = map handicap, 3 = total maps
    "counter-strike": ("cs2", {"1": "WIN", "2": "AH", "3": "OU"}),
    "dota-2": ("dota2", {"1": "WIN", "2": "AH", "3": "OU"}),
    "league-of-legends": ("lol", {"1": "WIN", "2": "AH", "3": "OU"}),
    "valorant": ("valorant", {"1": "WIN", "2": "AH", "3": "OU"}),
}
# virtual / simulated competitions that no other bookie has
SKIP = re.compile(r"simulated|srl|esoccer|ebasketball|cyber|virtual", re.IGNORECASE)

QUERY = """query Fixtures($sport: String!, $limit: Int, $offset: Int) {
  slugSport(sport: $sport) {
    fixtureList(type: upcoming, limit: $limit, offset: $offset) {
      id slug extId name status
      data { ... on SportFixtureDataMatch { startTime competitors { name } } }
      tournament { slug name category { slug name } }
      groups(groups: ["main", "1st2ndhalfmarkets", "goals"], status: [active]) {
        templates(limit: 30, includeEmpty: false) {
          extId
          markets(limit: 15) { status specifiers outcomes { name active odds } }
        }
      }
    }
  }
}"""

# Runs inside the page: pages through one sport and returns only the markets we use.
FETCH_JS = """
async ({query, sport, pageSize, maxPages, wanted}) => {
  const out = [];
  for (let pg = 0; pg < maxPages; pg++) {
    const r = await fetch('/_api/graphql', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-language': 'en'},
      body: JSON.stringify({query, variables: {sport, limit: pageSize, offset: pg * pageSize}}),
    });
    if (!r.ok) return {error: r.status, items: out};
    const body = await r.json();
    const list = (body.data && body.data.slugSport && body.data.slugSport.fixtureList) || [];
    for (const f of list) {
      const d = f.data || {};
      const markets = [];
      for (const g of f.groups || []) for (const t of g.templates || []) {
        if (!wanted.includes(t.extId)) continue;
        for (const m of t.markets || []) {
          if (m.status !== 'active') continue;
          markets.push([t.extId, m.specifiers || '',
                        (m.outcomes || []).map(o => [o.name, o.active ? o.odds : 0])]);
        }
      }
      out.push({id: f.id, slug: f.slug, br: f.extId, status: f.status, start: d.startTime,
                teams: (d.competitors || []).map(c => c.name),
                league: f.tournament && f.tournament.name, tslug: f.tournament && f.tournament.slug,
                category: f.tournament && f.tournament.category && f.tournament.category.slug,
                cname: f.tournament && f.tournament.category && f.tournament.category.name, markets});
    }
    if (list.length < pageSize) break;
  }
  return {items: out};
}
"""


class StakeScraper(Scraper):
    name = "Stake"
    region = "crypto"

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
        await self._page.wait_for_timeout(4000)
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
        for slug, (sport, templates) in SPORTS.items():
            try:
                res = await page.evaluate(FETCH_JS, {
                    "query": QUERY, "sport": slug, "pageSize": PAGE_SIZE, "maxPages": MAX_PAGES,
                    "wanted": list(templates),
                })
            except Exception:
                await self._reset_page()
                raise
            events += [ev for f in res["items"] if (ev := self._parse(f, slug, sport, templates))]
            if res.get("error"):
                await self._reset_page()
                if not events:
                    raise RuntimeError(f"Stake: HTTP {res['error']}")
                break
        return events

    def _parse(self, f: dict, slug: str, sport: str, templates: dict) -> Event | None:
        teams = f.get("teams") or []
        if len(teams) != 2 or not f.get("start") or f.get("status") != "active":
            return None
        if SKIP.search(f"{f.get('category')} {f.get('tslug')} {f.get('league')}"):
            return None
        home, away = teams
        br = (f.get("br") or "").rsplit(":", 1)[-1]
        ev = Event(
            bookie=self.name,
            event_id=f["id"],
            sport=sport,
            home=home,
            away=away,
            start=parsedate_to_datetime(f["start"]),
            league=f"{f.get('cname') or ''} {f.get('league') or ''}".strip(),
            betradar_id=br if br.isdigit() else None,
            url=f"https://stake.com/sports/{slug}/{f.get('category')}/{f.get('tslug')}/{f.get('slug')}",
        )
        for tid, spec, outcomes in f["markets"]:
            kind = templates.get(tid)
            odds = [o for _, o in outcomes]
            params = dict(p.split("=", 1) for p in spec.split("|") if "=" in p)
            prefix = ""
            if kind and kind[:3] in ("H1_", "T1_", "T2_"):
                prefix, kind = kind[:3], kind[3:]
            if kind == "1X2" and len(odds) == 3:
                for oc, o in zip(("1", "X", "2"), odds):
                    ev.add(prefix + "1X2", oc, o)
            elif kind in ("WIN", "WIN_OT") and len(odds) == 2 and params.get("way", "two") == "two":
                market = "12_OT" if kind == "WIN_OT" else "12"
                ev.add(market, "1", odds[0])
                ev.add(market, "2", odds[1])
            elif kind == "BTTS":
                for n, o in outcomes:
                    if n.lower() in ("yes", "no"):
                        ev.add("BTTS", "GG" if n.lower() == "yes" else "NG", o)
            elif kind == "DC":
                for n, o in outcomes:
                    oc = _dc_outcome(n, home, away)
                    if oc:
                        ev.add("DC", oc, o)
            elif kind == "OU":
                line = params.get("total") or params.get("threshold")
                by_side = {n.split(" ", 1)[0].lower(): o for n, o in outcomes}
                if line and "over" in by_side and "under" in by_side and clean_line(line):
                    ev.add(f"{prefix}OU_{line_str(line)}", "O", by_side["over"])
                    ev.add(f"{prefix}OU_{line_str(line)}", "U", by_side["under"])
            elif kind == "AH" and len(odds) == 2:
                line = params.get("hcp") or params.get("handicap")
                if line is not None and ":" not in line and clean_line(line):  # "0:1" = European 3-way
                    ev.add(f"{prefix}AH_{line_str(line)}", "1", odds[0])
                    ev.add(f"{prefix}AH_{line_str(line)}", "2", odds[1])
        return ev if ev.markets else None

    async def close(self) -> None:
        await self._reset_page()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


def _dc_outcome(name: str, home: str, away: str) -> str | None:
    """"Home or Draw" -> 1X, "Draw or Away" -> X2, "Home or Away" -> 12."""
    parts = [p.strip() for p in name.split(" or ")]
    if len(parts) != 2:
        return None
    code = {home: "1", "Draw": "X", away: "2"}
    a, b = code.get(parts[0]), code.get(parts[1])
    if not a or not b:
        return None
    oc = "".join(sorted((a, b), key="1X2".index))
    return oc if oc in ("1X", "12", "X2") else None
