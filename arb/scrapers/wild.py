"""Wild.io (crypto casino, BetConstruct sportsbook). Their own JSON API lists every
match with its main market only (1X2 / match winner), odds in thousandths.

Serbian ISPs block wild.io in DNS, so we look it up with Cloudflare's
DNS-over-HTTPS and connect to that IP (TLS still verified against wild.io)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

HOST = "wild.io"
PAGE = 500
# wild sport id -> (sport key in their URLs, our sport)
SPORTS = {
    304: ("soccer", "football"),
    306: ("basketball", "basketball"),
    307: ("tennis", "tennis"),
    305: ("ice-hockey", "hockey"),
    332: ("handball", "handball"),
    308: ("volleyball", "volleyball"),
    343: ("table-tennis", "table_tennis"),
}


async def resolve(host: str) -> str | None:
    """IP of `host` from Cloudflare's DNS-over-HTTPS. Serbian ISPs either don't resolve
    blocked casino domains or point them at their own block page (wrong certificate),
    so the system DNS can't be trusted here. None = DoH unavailable, use system DNS."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://cloudflare-dns.com/dns-query", params={"name": host, "type": "A"},
                            headers={"accept": "application/dns-json"})
            answers = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
    except (httpx.HTTPError, ValueError):
        return None
    return answers[0] if answers else None


class WildScraper(Scraper):
    name = "Wild.io"
    region = "crypto"

    def __init__(self) -> None:
        self.client = make_client(timeout=30)
        self._ip: str | None = None
        self._resolved = False

    async def _get(self, path: str, params: dict) -> dict:
        if not self._resolved:
            self._ip, self._resolved = await resolve(HOST), True
        if self._ip:
            r = await self.client.get(f"https://{self._ip}{path}", params=params,
                                      headers={"Host": HOST}, extensions={"sni_hostname": HOST})
        else:
            r = await self.client.get(f"https://{HOST}{path}", params=params)
        if r.status_code >= 500 or r.status_code == 403:
            self._resolved = False  # IP may have changed - look it up again next time
        r.raise_for_status()
        return r.json()

    async def fetch(self) -> list[Event]:
        results = await asyncio.gather(*(self._sport(sid) for sid in SPORTS), return_exceptions=True)
        events = [ev for r in results if not isinstance(r, Exception) for ev in r]
        if not events:
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]
        return events

    async def _sport(self, sid: int) -> list[Event]:
        out, page = [], 1
        while True:
            d = await self._get("/api/sports/matches", {
                "sport_id": sid, "locale": "en", "limit": PAGE, "page": page, "match_type": "match"})
            out += [ev for m in d.get("data") or [] if (ev := self._parse(m, sid))]
            pg = d.get("pagination") or {}
            if page >= pg.get("last_page", 1):
                return out
            page += 1

    def _parse(self, m: dict, sid: int) -> Event | None:
        key, sport = SPORTS[sid]
        comp = m.get("competitors") or {}
        home, away = (comp.get("home") or {}).get("name"), (comp.get("away") or {}).get("name")
        if not home or not away or m.get("status") != 0 or not m.get("start_time"):
            return None
        start = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00"))
        if start <= datetime.now(timezone.utc):
            return None
        ev = Event(
            bookie=self.name,
            event_id=str(m["id"]),
            sport=sport,
            home=home,
            away=away,
            start=start,
            league=(m.get("tournament") or {}).get("name") or "",
            url=f"https://wild.io/sports/{key}/{m.get('slug')}-m-{m['id']}",
        )
        odds = [o.get("odds", 0) / 1000 for o in ((m.get("main_market") or {}).get("outcomes") or [])]
        if len(odds) == 3 and sport in ("football", "hockey", "handball"):
            for oc, o in zip(("1", "X", "2"), odds):
                ev.add("1X2", oc, o)
        elif len(odds) == 2 and sport != "football":
            market = "12_OT" if sport == "basketball" else "12"
            if sport in ("tennis", "volleyball", "table_tennis", "basketball"):
                ev.add(market, "1", odds[0])
                ev.add(market, "2", odds[1])
        return ev if ev.markets else None

    async def close(self) -> None:
        await self.client.aclose()
