"""SX Bet (decentralized betting exchange, no KYC). Exchange prices = very low margin,
so it often has the best odds. Markets are public; the order book (V3) needs a free
API key, which the user enters once in the bot (🏦 Kladionice -> 🔑 SX Bet ključ).

Soccer type 1 markets are binary per outcome: "Team" / "Not team", "Tie" / "Not tie"
-> 1, X, 2 and their opposites = double chances (Not home = X2, Not tie = 12, ...).
Type 226 = winner incl. overtime (basketball), 52 = winner (tennis), 2 = total goals.
Best odds from the taker's side: decimal = 10^20 / percentageOdds.
Every outcome also keeps its market hash and side (Event.bet_ref) for automatic betting (arb.live.sxbet)."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from arb import secrets
from arb.models import Event, clean_line, line_str
from arb.scrapers.base import Scraper, make_client

API = "https://api.sx.bet"
REFRESH_SECONDS = 60
# sx sport id -> our sport
SPORTS = {5: "football", 1: "basketball", 6: "tennis", 3: "baseball", 8: "american_football"}


class SXBetScraper(Scraper):
    name = "SX Bet"
    region = "crypto"

    def __init__(self) -> None:
        self.client = make_client(timeout=30)
        self._cache: list[Event] = []
        self._cache_at = 0.0
        self._by_event: dict[str, list[dict]] = {}  # event id -> its markets (from the last full fetch)

    async def fetch_fresh(self) -> list[Event]:
        self._cache_at = 0.0
        return await self.fetch()

    async def fetch(self) -> list[Event]:
        key = secrets.get("sxbet_api_key")
        if not key:
            raise RuntimeError("nema API ključa (🏦 Kladionice → 🔑 SX Bet ključ)")
        if self._cache and time.time() - self._cache_at < REFRESH_SECONDS:
            return self._cache
        markets = []
        for sid in SPORTS:
            markets += await self._markets(sid)
        wanted = [m for m in markets if self._kind(m)]
        by_event: dict[str, list[dict]] = {}
        for m in wanted:
            by_event.setdefault(m["sportXeventId"], []).append(m)
        self._by_event = by_event
        best: dict[str, dict] = {}
        chunks = [wanted[i:i + 100] for i in range(0, len(wanted), 100)]
        for r in await asyncio.gather(*(self._best(key, c) for c in chunks)):
            best.update(r)
        events = self._build(wanted, best)
        self._cache, self._cache_at = events, time.time()
        return events

    async def fetch_events(self, event_ids: list[str]) -> list[Event] | None:
        """Re-check of an arb: one odds call for just these games' markets."""
        key = secrets.get("sxbet_api_key")
        if not key or not all(i in self._by_event for i in event_ids):
            return None
        markets = [m for i in event_ids for m in self._by_event[i]]
        return self._build(markets, await self._best(key, markets))

    async def _markets(self, sport_id: int) -> list[dict]:
        out, page_key = [], None
        for _ in range(40):
            params = {"onlyMainLine": "true", "sportIds": sport_id, "pageSize": 50}
            if page_key:
                params["paginationKey"] = page_key
            r = await self.client.get(f"{API}/markets/active", params=params)
            r.raise_for_status()
            d = r.json()["data"]
            out += d.get("markets") or []
            page_key = d.get("nextKey")
            if not page_key:
                break
        return out

    async def _best(self, key: str, markets: list[dict]) -> dict[str, dict]:
        r = await self.client.get(f"{API}/orders-v3/odds/best", headers={"x-sx-api-key": key}, params={
            "marketHashes": ",".join(m["marketHash"] for m in markets), "showTakerPerspective": "true"})
        if r.status_code == 401:
            raise RuntimeError("API ključ nije dobar (🏦 Kladionice → 🔑 SX Bet ključ)")
        r.raise_for_status()
        return {b["marketHash"]: b for b in (r.json().get("data") or {}).get("bestOdds") or []}

    @staticmethod
    def _kind(m: dict) -> str | None:
        sport = SPORTS.get(m.get("sportId"))
        if m.get("status") != "ACTIVE" or not sport:
            return None
        t = m.get("type")
        if sport == "football":
            return {1: "binary", 2: "total", 3: "spread"}.get(t)
        if t == 52 and sport == "tennis":
            return "winner"
        if t == 226 and sport in ("basketball", "baseball", "american_football"):
            return "winner"
        if t == 28 and sport in ("basketball", "baseball", "american_football"):
            return "total"
        if t == 342 and sport in ("basketball", "baseball", "american_football"):
            return "spread"
        return None

    def _build(self, markets: list[dict], best: dict[str, dict]) -> list[Event]:
        now = datetime.now(timezone.utc)
        events: dict[str, Event] = {}
        for m in markets:
            b = best.get(m["marketHash"])
            if not b:
                continue
            odd1, odd2 = _odd(b.get("outcomeOne")), _odd(b.get("outcomeTwo"))
            size1, size2 = _size(b.get("outcomeOne")), _size(b.get("outcomeTwo"))
            if odd1 and odd2 and 1 / odd1 + 1 / odd2 < 0.98:
                continue  # one book can't be an arb with itself: odds read the wrong way round
            start = datetime.fromtimestamp(m["gameTime"], tz=timezone.utc)
            if start <= now:
                continue
            sport = SPORTS[m["sportId"]]
            home, away = m["teamOneName"], m["teamTwoName"]
            ev = events.get(m["sportXeventId"])
            if ev is None:
                ev = events[m["sportXeventId"]] = Event(
                    bookie=self.name,
                    event_id=m["sportXeventId"],
                    sport=sport,
                    home=home,
                    away=away,
                    start=start,
                    league=m.get("leagueLabel") or "",
                    url=f"https://sx.bet/{_slug(m.get('sportLabel') or sport)}/{_slug(m.get('leagueLabel') or '')}"
                        f"/game-lines/{m['sportXeventId']}",
                )
            kind = self._kind(m)
            if kind == "binary":
                one = m.get("outcomeOneName")
                pair = {home: ("1", "X2"), "Tie": ("X", "12"), away: ("2", "1X")}.get(one)
                if pair:
                    ev.add("1X2", pair[0], odd1)
                    ev.add("DC", pair[1], odd2)
                    _limit(ev, ("1X2", pair[0]), size1)
                    _limit(ev, ("DC", pair[1]), size2)
                    _ref(ev, ("1X2", pair[0]), m, True)
                    _ref(ev, ("DC", pair[1]), m, False)
                    ev.how[("1X2", pair[0])] = f"„{m.get('outcomeOneName')}“"
                    ev.how[("DC", pair[1])] = f"„{m.get('outcomeTwoName')}“ (na tržištu {one} / {m.get('outcomeTwoName')})"
            elif kind == "total" and m.get("line") is not None:
                if (m.get("outcomeOneName") or "").lower().startswith("over") and clean_line(m["line"]):
                    ev.add_total(m["line"], odd1, odd2)
                    _limit(ev, (f"OU_{line_str(m['line'])}", "O"), size1)
                    _limit(ev, (f"OU_{line_str(m['line'])}", "U"), size2)
                    _ref(ev, (f"OU_{line_str(m['line'])}", "O"), m, True)
                    _ref(ev, (f"OU_{line_str(m['line'])}", "U"), m, False)
            elif kind == "spread" and m.get("line") is not None:
                # "line" belongs to outcome one ("Malta -1.5"), which is team one
                one = (m.get("outcomeOneName") or "").lower()
                if one.startswith(home.lower()) and clean_line(m["line"]):
                    ev.add_handicap(m["line"], odd1, odd2)
                    _limit(ev, (f"AH_{line_str(m['line'])}", "1"), size1)
                    _limit(ev, (f"AH_{line_str(m['line'])}", "2"), size2)
                    _ref(ev, (f"AH_{line_str(m['line'])}", "1"), m, True)
                    _ref(ev, (f"AH_{line_str(m['line'])}", "2"), m, False)
                elif one.startswith(away.lower()) and clean_line(m["line"]):
                    ev.add_handicap(-float(m["line"]), odd2, odd1)
                    _limit(ev, (f"AH_{line_str(-float(m['line']))}", "1"), size2)
                    _limit(ev, (f"AH_{line_str(-float(m['line']))}", "2"), size1)
                    _ref(ev, (f"AH_{line_str(-float(m['line']))}", "1"), m, False)
                    _ref(ev, (f"AH_{line_str(-float(m['line']))}", "2"), m, True)
            elif kind == "winner":
                market = "12_OT" if sport in ("basketball", "american_football") else "12"
                ev.add(market, "1", odd1)
                ev.add(market, "2", odd2)
                _limit(ev, (market, "1"), size1)
                _limit(ev, (market, "2"), size2)
                _ref(ev, (market, "1"), m, True)
                _ref(ev, (market, "2"), m, False)
        return [ev for ev in events.values() if ev.markets]

    async def close(self) -> None:
        await self.client.aclose()


def _slug(s: str) -> str:
    """SX's own url slug: spaces -> '-', other punctuation dropped ("ATP - Tokyo" -> "atp---tokyo")."""
    return re.sub(r"[^a-z0-9-]", "", s.lower().replace(" ", "-"))


# SX takes 1 % of the net profit of taker bets (singles): odds 1.47 really pay 1.4653
FEE = 0.01


def _odd(level: dict | None) -> float | None:
    """Taker decimal odds from a best-odds level (percentageOdds = implied prob x 10^20), after the fee."""
    try:
        p = int(level["percentageOdds"]) / 1e20
    except (TypeError, KeyError, ValueError):
        return None
    if not 0 < p < 1:
        return None
    return round(1 + (1 / p - 1) * (1 - FEE), 3)


def _size(level: dict | None) -> float | None:
    """How much a taker can stake at that best price (USDC, 6 decimals); more fills at worse odds."""
    try:
        return int(level["size"]) / 1e6
    except (TypeError, KeyError, ValueError):
        return None


def _limit(ev: Event, key: tuple[str, str], size: float | None) -> None:
    if size is not None and key[0] in ev.markets and key[1] in ev.markets[key[0]]:
        ev.limits[key] = size


def _ref(ev: Event, key: tuple[str, str], m: dict, one: bool) -> None:
    """What a bet on this outcome sends to SX (automatic betting, arb.live.sxbet): the market and
    whether it backs outcome one (True) or outcome two (False) of it."""
    if key[0] in ev.markets and key[1] in ev.markets[key[0]]:
        ev.bet_ref[key] = {"market": m["marketHash"], "one": one, "event": m["sportXeventId"]}
