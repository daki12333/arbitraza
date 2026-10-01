"""Meridian: the API needs a Bearer token. An anonymous token is embedded in the
HTML of any public page, so we grab it from there and refresh it periodically."""
from __future__ import annotations

import asyncio
import re
import time

from arb.models import SPORT_SLUGS, Event, utc_from_ms
from arb.scrapers.base import Scraper, make_client

PAGE_URL = "https://meridianbet.rs/sr/kladjenje/fudbal"
API_URL = "https://online.meridianbet.rs/betshop/api/v1/standard/sport/{sport}/leagues"
SPORTS = {58: "football", 55: "basketball", 56: "tennis", 59: "hockey",
          60: "handball", 54: "volleyball", 89: "table_tennis"}
TOKEN_RE = re.compile(r'access_token\\?":\\?"(eyJ[A-Za-z0-9_.-]+)')
TOKEN_MAX_AGE = 20 * 60
PARALLEL_PAGES = 5
MAX_PAGES = 200


class MeridianScraper(Scraper):
    name = "Meridian"

    def __init__(self) -> None:
        self.client = make_client({"Accept-Language": "sr"})
        self._token: str | None = None
        self._token_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if force or not self._token or time.time() - self._token_at > TOKEN_MAX_AGE:
                r = await self.client.get(PAGE_URL, headers={"Accept": "text/html"})
                r.raise_for_status()
                m = TOKEN_RE.search(r.text)
                if not m:
                    raise RuntimeError("Meridian: access_token not found in page HTML")
                self._token, self._token_at = m.group(1), time.time()
            return self._token

    async def _page(self, sport_id: int, page: int) -> list[dict]:
        url = API_URL.format(sport=sport_id)
        params = {"page": page, "time": "ALL"}
        for attempt in range(2):
            token = await self._get_token(force=attempt > 0)
            r = await self.client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                continue
            r.raise_for_status()
            return r.json()["payload"]["leagues"]
        raise RuntimeError("Meridian: unauthorized after token refresh")

    async def fetch(self) -> list[Event]:
        await self._get_token()
        results = await asyncio.gather(*(self._fetch_sport(sid) for sid in SPORTS), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[0]
        return [ev for r in results if not isinstance(r, Exception) for ev in r]

    async def _fetch_sport(self, sport_id: int) -> list[Event]:
        events: list[Event] = []
        page = 0
        while page < MAX_PAGES:
            batch = await asyncio.gather(*(self._page(sport_id, p) for p in range(page, page + PARALLEL_PAGES)))
            done = False
            for leagues in batch:
                if not any(lg.get("events") for lg in leagues):
                    done = True
                for lg in leagues:
                    for e in lg.get("events") or []:
                        ev = self._parse(e, lg.get("leagueName", ""), SPORTS[sport_id])
                        if ev:
                            events.append(ev)
            if done:
                break
            page += PARALLEL_PAGES
        return events

    def _parse(self, e: dict, league: str, sport: str) -> Event | None:
        h = e["header"]
        rivals = h.get("rivals") or []
        if len(rivals) != 2 or h.get("state") != "ACTIVE":
            return None
        br = (h.get("betradar") or {}).get("id")
        ev = Event(
            bookie=self.name,
            event_id=str(h["eventId"]),
            sport=sport,
            home=rivals[0],
            away=rivals[1],
            start=utc_from_ms(h["startTime"]),
            league=league,
            betradar_id=str(br) if br else None,
            # the site routes by event id; slugs are cosmetic
            url="https://meridianbet.rs/sr/kladjenje/{}/{}/{}/{}/{}".format(
                SPORT_SLUGS[sport],
                (h.get("region") or {}).get("slug") or "region",
                (h.get("league") or {}).get("slug") or "liga",
                h.get("rivalsSlug") or "mec",
                h["eventId"],
            ),
        )
        for pos in e.get("positions") or []:
            for g in pos.get("groups") or []:
                gname = (g.get("name") or "").strip().lower()
                for s in g.get("selections") or []:
                    if s.get("state") != "ACTIVE" or s.get("placeholder"):
                        continue
                    sname = (s.get("name") or "").strip().lower()
                    price = s.get("price")
                    if gname == "konačan ishod" and sname in ("1", "x", "2"):
                        ev.add("1X2", sname.upper(), price)  # football / hockey / handball: regular time
                    elif gname == "dupla šansa" and sname in ("1x", "12", "x2"):
                        ev.add("DC", sname.upper(), price)
                    elif gname.startswith("pobednik") and sname in ("1", "2"):
                        if sport in ("tennis", "volleyball", "table_tennis"):
                            ev.add("12", sname, price)
                        elif sport == "basketball" and "ot" in gname:  # "Pobednik (uklj.OT)"
                            ev.add("12_OT", sname, price)
                    elif sport == "football" and gname == "ukupno golova" and g.get("overUnder") in (1.5, 2.5, 3.5):
                        side = {"manje": "U", "više": "O", "vise": "O"}.get(sname)
                        if side:
                            ev.add(f"OU_{g['overUnder']}", side, price)
                    elif sport == "football" and gname == "oba tima daju gol" and sname in ("gg", "ng"):
                        ev.add("BTTS", sname.upper(), price)
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()
