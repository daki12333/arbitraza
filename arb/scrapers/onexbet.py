"""1xBet Serbia (1xbet.rs) "LineFeed" API. A list call returns at most 50 games,
so we list the championships per sport and fetch each one (~450 small requests).
To stay polite that full crawl runs at most every REFRESH_SECONDS; scans in
between reuse the last result. No Betradar id - matched by team names/time.

Odds: G=1 T=1/2/3 -> 1/X/2 (tennis 1/3) | G=8 T=4/5/6 -> 1X/12/X2 | G=101 T=401/402 -> basketball winner
incl. OT | G=17 T=9/10 -> over/under P | G=19 T=180/181 -> GG/NG"""
from __future__ import annotations

import asyncio
import re
import time

from arb.models import Event, clean_line, line_str, utc_from_ms
from arb.scrapers.base import Scraper, make_client, slugify

SPORTS = {1: "football", 3: "basketball", 4: "tennis", 2: "hockey", 8: "handball", 6: "volleyball", 10: "table_tennis"}
# crypto (1xBit) only: more sports; esports (40) are one "sport" here, the game is in the league name
CRYPTO_SPORTS = {5: "baseball", 13: "american_football", 40: "esports"}
ESPORT_PREFIX = {"cs 2.": "cs2", "cs2.": "cs2", "dota 2.": "dota2", "league of legends.": "lol", "lol.": "lol",
                 "valorant.": "valorant"}
SPORT_PATH = {1: "football", 3: "basketball", 4: "tennis", 2: "ice-hockey", 8: "handball", 6: "volleyball",
              10: "table-tennis", 5: "baseball", 13: "american-football", 40: "esports"}
REFRESH_SECONDS = 60
PARALLEL = 8
# championship "leagues" that hold side markets as fake games
SKIP_CHAMP = re.compile(r"statistic|special|corner|card|booking|foul|offside|shot|throw|goal kick|player|"
                        r"alternative|team vs|home vs|minute|penalt|outright|winner|esport|cyber|simulated",
                        re.IGNORECASE)
OU_LINES = {1.5, 2.5, 3.5}
PAGE = 50  # max games one Get1x2_VZip call returns
EXTRA_GAMES_HOURS = 48  # beyond the first 50 of a league, only fetch games starting within this window


class OneXBetScraper(Scraper):
    name = "1xBet"
    host = "https://1xbet.rs"
    site_lang = "en"

    def __init__(self) -> None:
        self.api = f"{self.host}/service-api/LineFeed"
        self.site = f"{self.host}/{self.site_lang}/line"
        self.client = make_client({"Referer": self.site}, timeout=30)
        self._cache: list[Event] = []
        self._cache_at = 0.0
        self._sem = asyncio.Semaphore(PARALLEL)
        # re-checks of single games get their own lane: behind a full crawl's thousands of
        # queued calls they would wait for minutes
        self._fast = asyncio.Semaphore(4)
        self.crypto = self.region == "crypto"  # more sports and lines only for the crypto mode
        self._sports = {**SPORTS, **CRYPTO_SPORTS} if self.crypto else SPORTS
        self._esport_of: dict[int, str] = {}  # esports championship id -> game

    async def fetch_events(self, event_ids: list[str]) -> list[Event]:
        games = await asyncio.gather(*(self._game(int(i), self._fast) for i in event_ids), return_exceptions=True)
        return [ev for g in games if isinstance(g, dict) and g.get("SI") in self._sports
                and (ev := self._parse(g, g["SI"]))]

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        jobs = []
        for sid in self._sports:
            r = await self.client.get(f"{self.api}/GetChampsZip", params={"sport": sid, "lng": "en"})
            r.raise_for_status()
            for ch in r.json().get("Value") or []:
                name = ch.get("L") or ""
                if sid == 40:  # esports: keep the games we know, by league name
                    game = next((g for p, g in ESPORT_PREFIX.items() if name.lower().startswith(p)), None)
                    if not game or "winner" in name.lower() or not ch.get("GC"):
                        continue
                    self._esport_of[ch["LI"]] = game
                    jobs.append(self._champ(sid, ch["LI"], ch["GC"]))
                elif ch.get("GC") and not SKIP_CHAMP.search(name):
                    jobs.append(self._champ(sid, ch["LI"], ch["GC"]))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events and results and isinstance(results[0], Exception):
            raise results[0]
        self._cache, self._cache_at = events, time.time()
        return events

    async def _champ(self, sport_id: int, champ_id: int, game_count: int) -> list[Event]:
        async with self._sem:
            r = await self.client.get(f"{self.api}/Get1x2_VZip", params={
                "sports": sport_id, "champs": champ_id, "count": PAGE, "lng": "en", "mode": 4, "getEmpty": "true",
            })
        r.raise_for_status()
        games = r.json().get("Value") or []
        if game_count > PAGE:
            # the list call stops at 50 games (big table tennis leagues have ~300):
            # fetch the rest one by one
            games += await self._remaining_games(champ_id, {g["I"] for g in games})
        events = [ev for g in games if (ev := self._parse(g, sport_id))]
        return events

    async def _remaining_games(self, champ_id: int, have: set[int]) -> list[dict]:
        async with self._sem:
            r = await self.client.get(f"{self.api}/GetChampZip", params={"champ": champ_id, "lng": "en"})
        r.raise_for_status()
        now = time.time()
        ids = [g["I"] for g in (r.json().get("Value") or {}).get("G") or []
               if g["I"] not in have and 0 < g.get("S", 0) - now < EXTRA_GAMES_HOURS * 3600]
        results = await asyncio.gather(*(self._game(i) for i in ids), return_exceptions=True)
        return [g for g in results if isinstance(g, dict)]

    async def _game(self, game_id: int, lane: asyncio.Semaphore | None = None) -> dict | None:
        async with lane or self._sem:
            r = await self.client.get(f"{self.api}/GetGameZip", params={
                "id": game_id, "lng": "en", "GroupEvents": "true", "countevents": 250, "grMode": 4, "marketType": 1,
            })
        r.raise_for_status()
        g = r.json().get("Value")
        if not g:
            return None
        # odds come grouped here ("GE"): flatten to the list call's shape
        g["E"] = [dict(e, G=ge.get("G")) for ge in g.get("GE") or [] for col in ge.get("E") or [] for e in col]
        return g

    def _parse(self, g: dict, sport_id: int) -> Event | None:
        sport = self._esport_of.get(g.get("LI")) if sport_id == 40 else self._sports.get(sport_id)
        if not sport:
            return None
        if not g.get("O1") or not g.get("O2") or g.get("S", 0) <= time.time() or g.get("TG"):
            return None
        ev = Event(
            bookie=self.name,
            event_id=str(g["I"]),
            sport=sport,
            home=g["O1"],
            away=g["O2"],
            start=utc_from_ms(g["S"] * 1000),
            league=g.get("L") or "",
            url=(f"{self.site}/{SPORT_PATH[sport_id]}/{g.get('LI')}-{slugify(g.get('L') or 'league')}/"
                 f"{g['I']}-{slugify(g['O1'])}-{slugify(g['O2'])}"),
        )
        for e in g.get("E") or []:
            if e.get("B"):  # blocked ("locked" on the site) - can't be bet
                continue
            grp, typ, odd, line = e.get("G"), e.get("T"), e.get("C"), e.get("P")

            def put(market: str, outcome: str) -> None:
                ev.add(market, outcome, odd)
                if outcome in ev.markets.get(market, {}):
                    # what a bet slip on the site sends for it (automatic betting, arb.live.onexbit)
                    ev.bet_ref[(market, outcome)] = {"GameId": g["I"], "Type": typ, "Param": line or 0, "Group": grp}

            if grp == 1 and sport in ("football", "hockey", "handball") and typ in (1, 2, 3):
                put("1X2", {1: "1", 2: "X", 3: "2"}[typ])
            elif grp == 8 and sport in ("football", "hockey") and typ in (4, 5, 6):
                put("DC", {4: "1X", 5: "12", 6: "X2"}[typ])
            elif grp == 1 and sport in ("tennis", "volleyball", "table_tennis", "baseball", "cs2", "dota2", "lol",
                                        "valorant") and typ in (1, 3):
                put("12", "1" if typ == 1 else "2")
            elif grp == 101 and sport in ("basketball", "american_football") and typ in (401, 402):
                put("12_OT", "1" if typ == 401 else "2")
            elif grp == 17 and typ in (9, 10) and (
                    (sport == "football" and line in OU_LINES)
                    or (self.crypto and sport in ("football", "basketball", "tennis", "handball", "baseball",
                                                  "american_football"))):
                if clean_line(line):
                    put(f"OU_{line_str(line)}", "O" if typ == 9 else "U")
            elif grp == 2 and typ in (7, 8) and self.crypto and line is not None and sport in (
                    "football", "basketball", "tennis", "handball", "baseball", "american_football"):
                home_line = line if typ == 7 else -line  # P = that side's own handicap
                if clean_line(home_line):
                    put(f"AH_{line_str(home_line)}", "1" if typ == 7 else "2")
            elif grp == 19 and sport == "football" and typ in (180, 181):
                put("BTTS", "GG" if typ == 180 else "NG")
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()


class VivatBetScraper(OneXBetScraper):
    """Same platform and (so far) the very same odds as 1xBet."""

    name = "VivatBet"
    host = "https://vivatbet.rs"
    site_lang = "sp"


class OneXBitScraper(OneXBetScraper):
    """1xBit: the crypto (BTC/USDT) sister site of 1xBet, same LineFeed platform."""

    name = "1xBit"
    region = "crypto"
    host = "https://1xbit.com"
    site_lang = "en"

