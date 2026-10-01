"""Polymarket (crypto prediction exchange, no KYC outside the US, not blocked in RS).
Sports games are binary markets priced like an exchange (very low margin), so its
odds are often the best on the market - exactly what creates arbs.

Public "gamma" API, plain HTTP: /sports lists every league with its series id and
tags; /events?series_id=... returns a league's open games with their markets.
Odds from the order book top: buying "Yes"/outcome A at bestAsk, "No"/outcome B at
1 - bestBid, minus the taker fee (see _odds_after_fee). Then the CLOB API's full order
books (POST /books) give every outcome its depth: how many $ sit at which price, so
a bigger stake gets the real, lower average odd (Event.depth). Soccer "Will X win?" = 1, "draw" = X, and the
"No" side of those gives the double chances (No on home win = X2, ...)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from arb.models import Event, line_str
from arb.scrapers.base import Scraper, make_client

log = logging.getLogger(__name__)

API = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BOOK_BATCH = 500  # order books per POST /books (1000 is "Payload exceeds the limit")
MAX_DEPTH_USD = 20_000  # read the book this deep per outcome
REFRESH_SECONDS = 90
PARALLEL = 8
# polymarket sport tag id -> our sport
SPORT_TAGS = {"100350": "football", "28": "basketball", "745": "basketball", "102669": "basketball",
              "864": "tennis", "100088": "hockey", "899": "hockey", "102897": "handball",
              "102883": "volleyball", "103767": "table_tennis",
              "678": "baseball", "100381": "baseball", "450": "american_football", "100351": "american_football",
              "100780": "cs2", "102366": "dota2", "65": "lol", "101672": "valorant"}
TWO_WAY = ("basketball", "tennis", "volleyball", "table_tennis", "baseball", "american_football",
           "cs2", "dota2", "lol", "valorant")
# winner incl. overtime / extra innings / all maps - the only result these sports have
WINNER_MARKET = {"basketball": "12_OT", "american_football": "12_OT"}


def _floats(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v or []


# Taker fee on sports markets: rate * p * (1 - p) per share bought at price p, paid
# on top of the price. A share pays 1 if it wins, so the real decimal odds are
# 1 / (p + fee). The rate differs per market (0, 0.03, 0.05 ...) and comes with it
# (feeSchedule.rate); FEE_RATE only when a market doesn't say.
FEE_RATE = 0.05


def _rate(m: dict) -> float:
    if m.get("feesEnabled") is False:
        return 0.0
    try:
        return float((m.get("feeSchedule") or {})["rate"])
    except (KeyError, TypeError, ValueError):
        return FEE_RATE


def _odds_after_fee(price: float, rate: float = FEE_RATE) -> float:
    return round(1 / (price + rate * price * (1 - price)), 3)


def _prices(m: dict) -> tuple[float | None, float | None] | None:
    """(odd for outcome A / "Yes", odd for outcome B / "No") from the book top, after the taker fee;
    None for a side nobody sells. A wide bid/ask gap is fine: the real order books are read
    afterwards (_add_depth) and the stake check only allows what can really be bought."""
    try:
        bid, ask = float(m.get("bestBid") or 0), float(m.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if m.get("closed") or not m.get("acceptingOrders", True):
        return None
    rate = _rate(m)
    yes = _odds_after_fee(ask, rate) if 0 < ask < 1 else None
    no = _odds_after_fee(1 - bid, rate) if 0 < bid < 1 else None
    return (yes, no) if yes or no else None


def _ladder(asks: list[dict], rate: float) -> list[tuple[float, float]]:
    """Sell orders of one outcome token -> [(odd after fee, $ it takes to buy them all), ...], best first."""
    levels = sorted((float(a["price"]), float(a["size"])) for a in asks)
    out, total = [], 0.0
    for price, size in levels:
        if not 0 < price < 1 or size <= 0:
            continue
        cost = price + rate * price * (1 - price)  # $ per share incl. fee
        out.append((round(1 / cost, 3), size * cost))
        total += size * cost
        if total >= MAX_DEPTH_USD:
            break
    return out


class PolymarketScraper(Scraper):
    name = "Polymarket"
    region = "crypto"
    fetch_timeout = 240  # ~1900 games + ~95k order books: ~40 s alone, much more while the bot matches a scan

    def __init__(self) -> None:
        self.client = make_client(timeout=30)
        self._sem = asyncio.Semaphore(PARALLEL)
        self._cache: list[Event] = []
        self._cache_at = 0.0
        # event id -> (ids of its gamma events incl. "- More Markets", league, sport): for fetch_events
        self._games: dict[str, tuple[list[str], dict, str]] = {}

    async def _get(self, path: str, params: dict | None = None):
        async with self._sem:
            r = await self.client.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        links: list[tuple] = []  # (event, market, outcome, token id, fee rate)
        leagues = []
        for lg in await self._get("/sports"):
            tags = str(lg.get("tags") or "").split(",")
            sport = next((SPORT_TAGS[t] for t in tags if t in SPORT_TAGS), None)
            if sport and lg.get("series"):
                leagues.append((lg, sport))
        results = await asyncio.gather(*(self._league(lg, sport, links) for lg, sport in leagues), return_exceptions=True)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events:
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]
        await self._add_depth(links)
        events = [ev for ev in events if ev.markets]
        self._cache, self._cache_at = events, time.time()
        return events

    async def fetch_events(self, event_ids: list[str]) -> list[Event] | None:
        """Re-check of an arb: only these games (~1 s) instead of every league (~20 s)."""
        if not all(i in self._games for i in event_ids):
            return None
        now = datetime.now(timezone.utc)
        links: list[tuple] = []

        async def one(eid: str) -> Event | None:
            ids, lg, sport = self._games[eid]
            parts = await asyncio.gather(*(self._get(f"/events/{pid}") for pid in ids))
            main = next(p for p in parts if str(p.get("id")) == eid)
            markets = [m for p in parts for m in p.get("markets") or []]
            return self._parse(main, markets, lg, sport, now, links)

        events = await asyncio.gather(*(one(i) for i in event_ids))
        await self._add_depth(links)
        return [ev for ev in events if ev and ev.markets]

    async def _add_depth(self, links: list[tuple]) -> None:
        """Full order book of every outcome we use: real odds for bigger stakes."""
        tokens = sorted({link[3] for link in links})
        books: dict[str, list] = {}

        async def batch(chunk: list[str]) -> None:
            async with self._sem:
                r = await self.client.post(f"{CLOB}/books", json=[{"token_id": tok} for tok in chunk])
            r.raise_for_status()
            for b in r.json():
                books[str(b.get("asset_id"))] = b.get("asks") or []

        results = await asyncio.gather(*(batch(tokens[i:i + BOOK_BATCH]) for i in range(0, len(tokens), BOOK_BATCH)),
                                       return_exceptions=True)
        if errs := [r for r in results if isinstance(r, Exception)]:
            log.warning("Polymarket: %d/%d order book calls failed (%s)", len(errs), len(results), errs[0])
        for ev, key, oc, token, rate in links:
            if token not in books:
                continue  # no answer for it: keep the price from the market list
            ladder = _ladder(books[token], rate)
            if not ladder:  # nobody is selling it: can't be bought right now
                ev.markets.get(key, {}).pop(oc, None)
                continue
            if oc in ev.markets.get(key, {}):
                ev.markets[key][oc] = ladder[0][0]
                ev.depth[(key, oc)] = ladder
        for ev in {id(link[0]): link[0] for link in links}.values():
            ev.markets = {k: v for k, v in ev.markets.items() if v}

    async def _league(self, lg: dict, sport: str, links: list) -> list[Event]:
        now = datetime.now(timezone.utc)
        games: dict[tuple, list[dict]] = {}
        for offset in range(0, 2000, 100):
            page = await self._get("/events", {"series_id": lg["series"], "closed": "false", "limit": 100,
                                               "offset": offset,
                                               "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
            for e in page:
                # "X vs. Y - More Markets" etc. are the same game with extra markets: merge them
                base = (e.get("title") or "").split(" - ")[0].strip()
                games.setdefault((base, e.get("startTime")), []).append(e)
            if len(page) < 100:
                break
        out = []
        for (base, _), parts in games.items():
            main = next((e for e in parts if " - " not in (e.get("title") or "")), None)
            if not main:
                continue
            markets = [m for e in parts for m in e.get("markets") or []]
            ev = self._parse(main, markets, lg, sport, now, links)
            if ev:
                out.append(ev)
                self._games[ev.event_id] = ([str(e["id"]) for e in parts], lg, sport)
        return out

    def _parse(self, e: dict, markets: list[dict], lg: dict, sport: str, now: datetime,
               links_out: list) -> Event | None:
        title = e.get("title") or ""
        if not e.get("startTime"):
            return None
        start = datetime.fromisoformat(e["startTime"].replace("Z", "+00:00"))
        if start <= now:
            return None
        teams = re.split(r"\s+vs\.?\s+", re.sub(r"^[^:]*:\s*", "", title), maxsplit=1)
        if len(teams) != 2:
            return None
        home, away = (t.strip() for t in teams)
        if lg.get("ordering") == "away":  # US style "Away vs. Home"
            home, away = away, home
        ev = Event(
            bookie=self.name,
            event_id=str(e["id"]),
            sport=sport,
            home=home,
            away=away,
            start=start,
            league=lg.get("name") or "",
            url=f"https://polymarket.com/event/{e.get('slug')}",
        )
        links = []

        def link(key: str, oc: str, m: dict, idx: int) -> None:
            """Remember which outcome token is bought for (key, oc) - for its order book."""
            toks = _floats(m.get("clobTokenIds"))
            if len(toks) == 2:
                links.append((key, oc, str(toks[idx]), _rate(m)))

        def is_home(name: str) -> bool | None:
            n = name.strip().lower()
            if n == home.lower() or n in home.lower() or home.lower() in n:
                return True
            if n == away.lower() or n in away.lower() or away.lower() in n:
                return False
            return None

        moneyline = [m for m in markets if m.get("sportsMarketType") == "moneyline"]
        if len(moneyline) == 3 and sport in ("football", "hockey", "handball"):
            for m in moneyline:
                p = _prices(m)
                if not p:
                    continue
                question = m.get("question") or ""
                q = question.lower()
                if "draw" in q:
                    pair = ("X", "12")
                elif home.lower() in q:
                    pair = ("1", "X2")
                elif away.lower() in q:
                    pair = ("2", "1X")
                else:
                    continue
                ev.add("1X2", pair[0], p[0])
                ev.add("DC", pair[1], p[1])
                link("1X2", pair[0], m, 0)
                link("DC", pair[1], m, 1)
                ev.how[("1X2", pair[0])] = f"kupi „Yes“ na „{question}“"
                ev.how[("DC", pair[1])] = f"kupi „No“ na „{question}“"
        elif len(moneyline) == 1 and sport in TWO_WAY:
            m = moneyline[0]
            names = [str(x) for x in _floats(m.get("outcomes"))]
            p = _prices(m)
            if p and len(names) == 2 and is_home(names[0]) is not None:
                market = WINNER_MARKET.get(sport, "12")
                a, b = (p[0], p[1]) if is_home(names[0]) else (p[1], p[0])
                ev.add(market, "1", a)
                ev.add(market, "2", b)
                link(market, "1", m, 0 if is_home(names[0]) else 1)
                link(market, "2", m, 1 if is_home(names[0]) else 0)

        halftime = [m for m in markets if m.get("sportsMarketType") == "soccer_halftime_result"]
        if len(halftime) == 3 and sport == "football":
            for m in halftime:
                p = _prices(m)
                question = m.get("question") or ""
                q = question.lower()
                if not p:
                    continue
                if "draw" in q:
                    pair = ("X", "12")
                elif q.startswith(home.lower()) or home.lower() in q:
                    pair = ("1", "X2")
                elif away.lower() in q:
                    pair = ("2", "1X")
                else:
                    continue
                ev.add("H1_1X2", pair[0], p[0])
                ev.add("H1_DC", pair[1], p[1])
                link("H1_1X2", pair[0], m, 0)
                link("H1_DC", pair[1], m, 1)
                ev.how[("H1_1X2", pair[0])] = f"kupi „Yes“ na „{question}“"
                ev.how[("H1_DC", pair[1])] = f"kupi „No“ na „{question}“"

        for m in markets:
            kind = m.get("sportsMarketType")
            p = _prices(m)
            if kind == "both_teams_to_score" and sport == "football" and p:
                names = [str(x).lower() for x in _floats(m.get("outcomes"))]
                if names[:2] == ["yes", "no"]:
                    ev.add("BTTS", "GG", p[0])
                    ev.add("BTTS", "NG", p[1])
                    link("BTTS", "GG", m, 0)
                    link("BTTS", "NG", m, 1)
                continue
            names = [str(x) for x in _floats(m.get("outcomes"))]
            if not p or len(names) != 2 or m.get("line") is None:
                continue
            line = float(m["line"])
            if line % 1 != 0.5:
                continue  # whole lines: a push resolves 50/50 here, not as a refund - not arb-safe
            question = m.get("question") or ""
            if kind == "totals" or (kind == "tennis_match_totals" and sport == "tennis") or (
                    kind in ("first_half_totals", "soccer_team_totals") and sport == "football"):
                if names[0].lower() != "over" or sport == "hockey":  # hockey totals: OT unclear
                    continue
                prefix = "H1_" if kind == "first_half_totals" else ""
                if kind == "soccer_team_totals":
                    who = (m.get("groupItemTitle") or "").rsplit(" O/U", 1)[0]
                    side = is_home(who)
                    if side is None:
                        continue
                    prefix = "T1_" if side else "T2_"
                key = f"{prefix}OU_{line_str(line)}"
                ev.add(key, "O", p[0])
                ev.add(key, "U", p[1])
                link(key, "O", m, 0)
                link(key, "U", m, 1)
                ev.how[(key, "O")] = f"„Over“ na „{question}“"
                ev.how[(key, "U")] = f"„Under“ na „{question}“"
            elif kind == "spreads" or (kind == "tennis_game_handicap" and sport == "tennis"):
                if sport == "hockey":
                    continue
                first_home = is_home(names[0])
                if first_home is None:
                    continue
                # "line" is the first outcome's handicap
                home_line = line if first_home else -line
                h, a = (p[0], p[1]) if first_home else (p[1], p[0])
                ev.add_handicap(home_line, h, a)
                key = f"AH_{line_str(home_line)}"
                link(key, "1", m, 0 if first_home else 1)
                link(key, "2", m, 1 if first_home else 0)
                ev.how[(key, "1")] = f"„{names[0] if first_home else names[1]}“ na „{question}“"
                ev.how[(key, "2")] = f"„{names[1] if first_home else names[0]}“ na „{question}“"
        if not ev.markets:
            return None
        links_out += [(ev, key, oc, tok, rate) for key, oc, tok, rate in links if oc in ev.markets.get(key, {})]
        return ev

    async def close(self) -> None:
        await self.client.aclose()
