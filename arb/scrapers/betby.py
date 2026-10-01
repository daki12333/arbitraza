"""Betby: the white-label sportsbook behind many crypto casinos (BC.Game, BetFury,
Rainbet, Betpanda, ...). Every brand has a public prematch feed on sptpub.com:
/en/0 lists "versions", each version is one chunk of events with all their odds.
Plain HTTP, no Cloudflare, ~0.5 s per brand.

Markets use Betradar ids: 1 = 1x2 (outcomes 1/2/3), 10 = double chance (9/10/11),
29 = both teams to score (74/76), 18 = total (12 over / 13 under),
186 = winner, 219 = winner incl. overtime (4/5)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

# betby sport id -> our sport
SPORTS = {"1": "football", "2": "basketball", "5": "tennis", "4": "hockey", "6": "handball",
          "23": "volleyball", "20": "table_tennis", "3": "baseball", "16": "american_football"}
# Betradar handicap / total market ids per sport (outcomes 1714/1715 home/away, 12/13 over/under)
HANDICAP = {"football": "16", "handball": "16", "basketball": "223", "tennis": "187", "baseball": "256",
            "american_football": "223"}
TOTALS = {"football": "18", "handball": "18", "basketball": "225", "tennis": "189", "baseball": "258",
          "american_football": "225"}
X12 = {"1": "1", "2": "X", "3": "2"}
DC = {"9": "1X", "10": "12", "11": "X2"}
BTTS = {"74": "GG", "76": "NG"}
TOTAL = {"12": "O", "13": "U"}
WINNER = {"4": "1", "5": "2"}
# "matches" that are really a whole series (odds = who wins the best-of-3), never the game itself
SKIP_TOURNAMENT = re.compile(r"series result|best of \d|outright|simulated|srl", re.IGNORECASE)


class BetbyScraper(Scraper):
    name = "Betby"
    region = "crypto"
    api = ""  # https://api-x-....sptpub.com
    brand = ""

    def __init__(self) -> None:
        self.client = make_client(timeout=30)

    def match_url(self, path: str) -> str:
        """path = /soccer/country/league/home-away-<id>"""
        raise NotImplementedError

    async def fetch(self) -> list[Event]:
        base = f"{self.api}/api/v4/prematch/brand/{self.brand}/en"
        r = await self.client.get(f"{base}/0")
        r.raise_for_status()
        idx = r.json()
        versions = idx.get("top_events_versions", []) + idx.get("rest_events_versions", [])
        chunks = await asyncio.gather(*(self.client.get(f"{base}/{v}") for v in versions))
        sports, cats, tours, events = {}, {}, {}, {}
        for c in chunks:
            c.raise_for_status()
            d = c.json()
            sports.update(d.get("sports") or {})
            cats.update(d.get("categories") or {})
            tours.update(d.get("tournaments") or {})
            events.update(d.get("events") or {})
        now = datetime.now(timezone.utc)
        out = []
        for eid, e in events.items():
            ev = self._parse(eid, e, sports, cats, tours, now)
            if ev:
                out.append(ev)
        return out

    def _parse(self, eid: str, e: dict, sports: dict, cats: dict, tours: dict, now: datetime) -> Event | None:
        desc, state = e.get("desc") or {}, e.get("state") or {}
        sport = SPORTS.get(desc.get("sport"))
        teams = desc.get("competitors") or []
        if not sport or desc.get("type") != "match" or len(teams) != 2 or not e.get("markets"):
            return None
        if any("⁽" in (c.get("name") or "") for c in teams):
            return None  # "Patriots ⁽ᵉ⁾" = simulated e-sport version of the real match
        if state.get("status", 0) != 0 or state.get("match_status", 0) != 0:
            return None  # suspended / already running
        start = datetime.fromtimestamp(desc["scheduled"], tz=timezone.utc)
        if start <= now:
            return None
        cat = cats.get(desc.get("category")) or {}
        tour = tours.get(desc.get("tournament")) or {}
        if SKIP_TOURNAMENT.search(tour.get("name", "")):
            return None
        path = "/".join([
            "", (sports.get(desc["sport"]) or {}).get("slug", ""), cat.get("slug", ""), tour.get("slug", ""),
            f"{desc.get('slug', '')}-{eid}",
        ])
        ev = Event(
            bookie=self.name,
            event_id=eid,
            sport=sport,
            home=teams[0]["name"],
            away=teams[1]["name"],
            start=start,
            league=f"{cat.get('name', '')} {tour.get('name', '')}".strip(),
            url=self.match_url(path),
        )
        m = e["markets"]
        def k(market: str, spec: str, oc: str):
            o = ((m.get(market) or {}).get(spec) or {}).get(oc) or {}
            return None if o.get("b") else o.get("k")  # "b" = blocked/suspended
        for oc, ours in X12.items():
            ev.add("1X2", ours, k("1", "", oc))
        if sport in ("basketball", "american_football"):
            for mid in ("219", "186"):  # a 2-way winner here always includes overtime
                for oc, ours in WINNER.items():
                    ev.add("12_OT", ours, k(mid, "", oc))
        elif sport in ("tennis", "volleyball", "table_tennis"):
            for oc, ours in WINNER.items():
                ev.add("12", ours, k("186", "", oc))
        elif sport == "baseball":  # winner incl. extra innings
            for oc, ours in WINNER.items():
                ev.add("12", ours, k("251", "", oc))
        if sport in HANDICAP:
            for spec in (m.get(HANDICAP[sport]) or {}):
                if spec.startswith("hcp=") and ":" not in spec:
                    ev.add_handicap(spec[4:], k(HANDICAP[sport], spec, "1714"), k(HANDICAP[sport], spec, "1715"))
        if sport in TOTALS and sport != "football":
            for spec in (m.get(TOTALS[sport]) or {}):
                if spec.startswith("total="):
                    ev.add_total(spec[6:], k(TOTALS[sport], spec, "12"), k(TOTALS[sport], spec, "13"))
        if sport in ("football", "hockey", "handball"):
            for oc, ours in DC.items():
                ev.add("DC", ours, k("10", "", oc))
        if sport == "football":
            for oc, ours in BTTS.items():
                ev.add("BTTS", ours, k("29", "", oc))
            for spec in (m.get("18") or {}):
                if spec.startswith("total="):
                    ev.add_total(spec[6:], k("18", spec, "12"), k("18", spec, "13"))
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()


class BCGameScraper(BetbyScraper):
    name = "BC.Game"
    api = "https://api-k-c7818b61-623.sptpub.com"
    brand = "2103509236163162112"

    def match_url(self, path: str) -> str:
        return f"https://bc.game/sports{path}"


class BetFuryScraper(BetbyScraper):
    name = "BetFury"
    api = "https://api-g-c7818b61-607.sptpub.com"
    brand = "2014049006031876096"

    def match_url(self, path: str) -> str:
        return f"https://betfury.com/sports{path}"


class RainbetScraper(BetbyScraper):
    name = "Rainbet"
    api = "https://api-a-c7818b61-600.sptpub.com"
    brand = "2374656571012681728"

    def match_url(self, path: str) -> str:
        return f"https://rainbet.com/sportsbook{path}"


class BetpandaScraper(BetbyScraper):
    name = "Betpanda"
    api = "https://api-a-c7818b61-600.sptpub.com"
    brand = "2384090534298923008"

    def match_url(self, path: str) -> str:
        return f"https://betpandacasino.io/en/sportsbook/?bt-path={quote(path, safe='')}"


class BetplayScraper(BetbyScraper):
    name = "Betplay"
    api = "https://api-g-c7818b61-607.sptpub.com"
    brand = "2442700428211785736"

    def match_url(self, path: str) -> str:
        return f"https://betplay.io/en/sportsbook/?bt-path={quote(path, safe='')}"


class GoldenPandaScraper(BetbyScraper):
    name = "Golden Panda"
    api = "https://api-h-c7818b61-608.sptpub.com"
    brand = "2432251421496844288"

    def match_url(self, path: str) -> str:
        return f"https://www.goldenpanda.com/sports{path}"


class ThrillScraper(BetbyScraper):
    name = "Thrill"
    api = "https://api-h-c7818b61-608.sptpub.com"
    brand = "2527459332061274117"

    def match_url(self, path: str) -> str:
        return f"https://thrill.com/sports{path}"


class FlushScraper(BetbyScraper):
    name = "Flush"
    api = "https://api-a-c7818b61-600.sptpub.com"
    brand = "2511187282057371656"

    def match_url(self, path: str) -> str:
        return f"https://flush.com/sports{path}"

