"""Cloudbet (crypto sportsbook). Their site's own JSON API, plain HTTP: one call per
sport returns every upcoming event with the markets we ask for. Events carry the
Betradar id. Serbian ISPs block cloudbet.com in DNS, so we connect to the IP from
Cloudflare's DNS-over-HTTPS (see wild.resolve)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from arb.models import Event, clean_line, line_str
from arb.scrapers.base import Scraper, make_client
from arb.scrapers.wild import resolve

HOST = "www.cloudbet.com"
DAYS_AHEAD = 14
REFRESH_SECONDS = 120  # the API rate-limits (429)
# cloudbet sport key -> (our sport, markets to request)
SPORTS = {
    "soccer": ("football", ["soccer.match_odds", "soccer.double_chance", "soccer.both_teams_to_score",
                            "soccer.total_goals", "soccer.asian_handicap", "soccer.match_odds_period_first_half",
                            "soccer.total_goals_period_first_half", "soccer.asian_handicap_period_first_half",
                            "soccer.team_total_goals"]),
    "basketball": ("basketball", ["basketball.moneyline", "basketball.handicap", "basketball.totals"]),
    "tennis": ("tennis", ["tennis.winner", "tennis.game_handicap", "tennis.total_games"]),
    "ice-hockey": ("hockey", ["ice_hockey.1x2", "ice_hockey.double_chance"]),
    "handball": ("handball", ["handball.match_odds"]),
    "volleyball": ("volleyball", ["volleyball.winner"]),
    "table-tennis": ("table_tennis", ["table_tennis.winner"]),
    "baseball": ("baseball", ["baseball.moneyline", "baseball.run_line", "baseball.totals"]),
    "american-football": ("american_football", ["american_football.moneyline", "american_football.handicap",
                                                "american_football.totals"]),
    "counter-strike": ("cs2", ["counter_strike.winner"]),
    "dota-2": ("dota2", ["dota_2.winner"]),
    "league-of-legends": ("lol", ["league_of_legends.winner"]),
    "valorant": ("valorant", ["valorant.winner"]),
}
TOTAL_KINDS = ("total_goals", "totals", "total_games")
HANDICAP_KINDS = ("asian_handicap", "handicap", "run_line", "game_handicap")
OT_SPORTS = ("basketball", "american_football")  # their 2-way winner includes overtime
X12 = {"home": "1", "draw": "X", "away": "2"}
DC = {"home_or_draw": "1X", "home_or_away": "12", "draw_or_away": "X2"}


class CloudbetScraper(Scraper):
    name = "Cloudbet"
    region = "crypto"

    def __init__(self) -> None:
        self.client = make_client(timeout=60)
        self._ip: str | None = None
        self._resolved = False
        self._cache: list[Event] = []
        self._cache_at = 0.0

    async def _get(self, path: str, params: list) -> dict:
        if not self._resolved:
            self._ip, self._resolved = await resolve(HOST), True
        if self._ip:
            r = await self.client.get(f"https://{self._ip}{path}", params=params,
                                      headers={"Host": HOST}, extensions={"sni_hostname": HOST})
        else:
            r = await self.client.get(f"https://{HOST}{path}", params=params)
        if r.status_code >= 500 or r.status_code == 403:
            self._resolved = False
        r.raise_for_status()
        return r.json()

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        results = []
        for k in SPORTS:  # one call at a time with a pause: Cloudflare blocks bursts (error 1015)
            try:
                results.append(await self._sport(k))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    raise  # blocked: stop now, the scanner waits as long as the site asks
                results.append(e)
            except Exception as e:
                results.append(e)
            await asyncio.sleep(2)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events:
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]
        self._cache, self._cache_at = events, time.time()
        return events

    async def _sport(self, key: str) -> list[Event]:
        sport, markets = SPORTS[key]
        now = int(time.time())
        base = [("from", now), ("to", now + DAYS_AHEAD * 86400), ("include-all-event-types", "false"),
                ("include-pretrading", "false"), ("limit", 999), ("live", "false"), ("locale", "en")]
        # the API rejects too many markets in one call: ask in chunks and merge per event
        chunks = [markets[i:i + 5] for i in range(0, len(markets), 5)]
        replies = []
        for c in chunks:  # one after another (rate limit)
            replies.append(await self._get("/sports-api/c/v6/sports/events", base + [("markets", m) for m in c] + [("sports", key)]))
            if len(chunks) > 1:
                await asyncio.sleep(2)
        events: dict[int, tuple[dict, dict]] = {}
        for d in replies:
            for sp in d.get("sports") or []:
                for comp in sp.get("competitions") or []:
                    for e in comp.get("events") or []:
                        if e["id"] in events:
                            events[e["id"]][0].setdefault("markets", {}).update(e.get("markets") or {})
                        else:
                            events[e["id"]] = (e, comp)
        return [ev for e, comp in events.values() if (ev := self._parse(e, comp, key, sport))]

    def _parse(self, e: dict, comp: dict, key: str, sport: str) -> Event | None:
        home, away = (e.get("home") or {}).get("name"), (e.get("away") or {}).get("name")
        if not home or not away or e.get("status") != "TRADING" or not e.get("cutoffTime"):
            return None
        if (e.get("metadata") or {}).get("eventStatus") not in (None, "", "not_started"):
            return None
        start = datetime.fromisoformat(e["cutoffTime"].replace("Z", "+00:00"))
        if start <= datetime.now(timezone.utc):
            return None
        comp_key = (comp.get("key") or "").removeprefix(f"{key}-")
        cat = (comp.get("category") or {}).get("name", "")
        ev = Event(
            bookie=self.name,
            event_id=str(e["id"]),
            sport=sport,
            home=home,
            away=away,
            start=start,
            league=f"{cat} {comp.get('name', '')}".strip(),
            betradar_id=str(e["betradarId"]) if e.get("betradarId") else None,
            url=f"https://www.cloudbet.com/en/sports/{key}/{comp_key}/{e['id']}",
        )
        for mkey, market in (e.get("markets") or {}).items():
            kind = mkey.split(".", 1)[1]
            prefix = ""
            if kind.endswith("_period_first_half"):
                kind, prefix = kind.removesuffix("_period_first_half"), "H1_"
            for sub_key, sub in (market.get("submarkets") or {}).items():
                if kind == "team_total_goals":
                    team = "T1_" if "team=home" in sub_key else "T2_" if "team=away" in sub_key else None
                    if team is None or not sub_key.startswith("period=ft"):
                        continue
                    for s in sub.get("selections") or []:
                        line = (s.get("params") or "").rsplit("total=", 1)[-1]
                        if s.get("status") == "SELECTION_ENABLED" and s.get("outcome") in ("over", "under") and clean_line(line):
                            ev.add(f"{team}OU_{line_str(line)}", "O" if s["outcome"] == "over" else "U", s.get("price"))
                    continue
                if prefix and sub_key != "period=1h":
                    continue
                if not prefix and sport in ("football", "hockey", "handball") and sub_key != "period=ft":
                    continue  # halves / periods
                for s in sub.get("selections") or []:
                    if s.get("status") != "SELECTION_ENABLED" or s.get("side", "BACK") != "BACK":
                        continue
                    oc, price, params = s.get("outcome"), s.get("price"), s.get("params") or ""
                    before = {k: dict(v) for k, v in ev.markets.items()}
                    if kind in ("match_odds", "1x2") and oc in X12 and sport in ("football", "hockey", "handball"):
                        ev.add(prefix + "1X2", X12[oc], price)
                    elif kind == "double_chance" and oc in DC:
                        ev.add("DC", DC[oc], price)
                    elif kind == "both_teams_to_score" and oc in ("yes", "no"):
                        ev.add("BTTS", "GG" if oc == "yes" else "NG", price)
                    elif kind in TOTAL_KINDS and oc in ("over", "under") and params.startswith("total="):
                        line = params[6:]
                        if clean_line(line):
                            ev.add(f"{prefix}OU_{line_str(line)}", "O" if oc == "over" else "U", price)
                    elif kind in HANDICAP_KINDS and oc in ("home", "away") and params.startswith("handicap="):
                        line = params[9:]  # the home side's handicap, for both selections
                        if clean_line(line):
                            ev.add(f"{prefix}AH_{line_str(line)}", "1" if oc == "home" else "2", price)
                    elif kind in ("moneyline", "winner") and oc in ("home", "away") and sport not in ("football", "hockey", "handball"):
                        ev.add("12_OT" if sport in OT_SPORTS else "12", "1" if oc == "home" else "2", price)
                    if s.get("maxStake") is not None:  # remember the limit of whatever was just added
                        for mk, outs in ev.markets.items():
                            for o in outs:
                                if o not in before.get(mk, {}):
                                    ev.limits[(mk, o)] = float(s["maxStake"])
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()
