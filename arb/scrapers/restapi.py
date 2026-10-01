"""The shared "restapi" platform (/restapi/offer/...): MaxBet, Soccerbet,
Merkur X-Tip, Oktagon, BetOle, BrazilBet, 365.rs.

Odds are keyed by "tip type" codes that are the same on every site, per sport:
  football  1/2/3 = 1X2 | 21/242, 22/24, 219/25 = under/over 1.5/2.5/3.5 | 272/273 = GG/NG
  basketball 50291/50293 = winner incl. OT | 1/2/3 = 1X2 regular time
  tennis    1/3 = match winner
  hockey    1/2/3 = 1X2 regular time
  football + hockey 7/8/9 = double chance 1X/12/X2
  handball (HB) 1/2/3 = 1X2 | table tennis (TT) 1/3 = match winner
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from arb.models import SPORT_SLUGS, Event, utc_from_ms
from arb.scrapers.base import Scraper, make_client, slugify

X12 = {"1": ("1X2", "1"), "2": ("1X2", "X"), "3": ("1X2", "2")}
DC = {"7": ("DC", "1X"), "8": ("DC", "12"), "9": ("DC", "X2")}

# platform sport code -> (our sport, {tip code: (market, outcome)})
SPORT_TIPS: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "S": ("football", {
        **X12, **DC,
        "21": ("OU_1.5", "U"), "242": ("OU_1.5", "O"),
        "22": ("OU_2.5", "U"), "24": ("OU_2.5", "O"),
        "219": ("OU_3.5", "U"), "25": ("OU_3.5", "O"),
        "272": ("BTTS", "GG"), "273": ("BTTS", "NG"),
    }),
    "B": ("basketball", {**X12, "50291": ("12_OT", "1"), "50293": ("12_OT", "2")}),
    "T": ("tennis", {"1": ("12", "1"), "3": ("12", "2")}),
    "H": ("hockey", {**X12, **DC}),
    "HB": ("handball", X12),
    "TT": ("table_tennis", {"1": ("12", "1"), "3": ("12", "2")}),
}


class RestApiScraper(Scraper):
    base_url: str = ""
    api_url: str = ""  # when the API lives on another host than the site
    desktop_version: str = "2.36.3.9"
    annex: str = "0"

    def __init__(self) -> None:
        self.client = make_client({"Referer": self.base_url + "/"})

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._fetch_sport(code) for code in SPORT_TIPS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, code: str) -> list[Event]:
        sport, tips = SPORT_TIPS[code]
        r = await self.client.get(
            f"{self.api_url or self.base_url}/restapi/offer/sr/sport/{code}/mob",
            params={"annex": self.annex, "desktopVersion": self.desktop_version, "locale": "sr"},
        )
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        events = []
        for m in r.json().get("esMatches", []):
            # "live" here means "will also be offered in-play", not "in progress":
            # matches get it hours before kickoff, so only the start time decides
            if m.get("blocked"):
                continue
            start = utc_from_ms(m["kickOffTime"])
            if start <= now:
                continue
            ev = Event(
                bookie=self.name,
                event_id=str(m["id"]),
                sport=sport,
                home=m["home"],
                away=m["away"],
                start=start,
                league=m.get("leagueName") or "",
                betradar_id=str(m["brMatchId"]) if m.get("brMatchId") else None,
                url=self.match_url(m, code, sport),
            )
            odds = self._odds(m)
            for tip, (market, outcome) in tips.items():
                ev.add(market, outcome, odds.get(tip))
            if ev.markets:
                events.append(ev)
        return events

    # e.g. .../fudbal/S/<league-slug>/<leagueId>/<home>-v-<away>/<matchId>
    match_path = "/sr/sportsko-kladjenje/{sport}/{code}/{league}/{league_id}/{teams}/{id}"

    def match_url(self, m: dict, code: str, sport: str) -> str:
        return self.base_url + self.match_path.format(
            sport=SPORT_SLUGS[sport],
            code=code,
            league=slugify(m.get("leagueName") or "liga"),
            league_id=m.get("leagueId"),
            teams=f"{slugify(m['home'])}-v-{slugify(m['away'])}",
            id=m["id"],
        )

    @staticmethod
    def _odds(m: dict) -> dict[str, float]:
        # MaxBet: flat {"code": odd}; Soccerbet: {"code": {"NULL": {"ov": odd, ...}}}
        if "odds" in m:
            return m["odds"]
        out = {}
        for code, by_spec in (m.get("betMap") or {}).items():
            pick = by_spec.get("NULL")
            if pick:
                out[code] = pick.get("ov")
        return out

    async def close(self) -> None:
        await self.client.aclose()


class MaxBetScraper(RestApiScraper):
    name = "MaxBet"
    base_url = "https://www.maxbet.rs"
    desktop_version = "1.2.1.10"
    annex = "3"
    match_path = "/sr/sportsko-kladjenje/{sport}/{code}/{league}/{league_id}/specijal/{teams}/{id}"


class SoccerbetScraper(RestApiScraper):
    name = "Soccerbet"
    base_url = "https://www.soccerbet.rs"
    match_path = "/sr/sportsko-kladjenje/ponuda/mec/{sport}/{code}/{league}/{league_id}/{teams}/{id}"


class MerkurXTipScraper(RestApiScraper):
    name = "MerkurXTip"
    base_url = "https://www.merkurxtip.rs"
    match_path = "/sr/sportsko-kladjenje/{sport}/{code}/{league}/{league_id}/special/{teams}/{id}"


class BrazilBetScraper(RestApiScraper):
    name = "BrazilBet"
    base_url = "https://www.brazilbet.rs"
    match_path = "/sr/sportsko-kladjenje/{sport}/{code}/{league}/{league_id}/special/{teams}/{id}"


class BetOleScraper(RestApiScraper):
    name = "BetOle"
    base_url = "https://www.betole.com"
    match_path = "/match-special/{id}"


class OktagonScraper(RestApiScraper):
    name = "Oktagon"
    base_url = "https://www.oktagonbet.com"
    match_path = "/ibet-web-client/#/home/special/{id}/{code}"


class Kladionica365Scraper(RestApiScraper):
    name = "365.rs"
    base_url = "https://www.365.rs"
    api_url = "https://ibet2.365.rs"
    match_path = "/prematch-special/{code}/{id}"
