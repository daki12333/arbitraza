"""Topbet: its sportsbook (sportbook.topbetbo.com, embedded in topbet.rs)
publishes the whole pre-match offer as one gzipped JSON file. meta.id_external
is the Betradar id. Start times are local (Belgrade) without an offset.

Offer codes: F1/FX/F2 = 1X2 (tennis F1/F2 = winner) | F_OT_1/F_OT_2 = basketball
winner incl. OT | o0_1/o2p, o0_2/o3p, o0m3/o4p = goals 0-1/2+, 0-2/3+, 0-3/4+ | GG/NG"""
from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from arb.models import Event
from arb.scrapers.base import Scraper, make_client

OFFER_URL = "https://sportbook.topbetbo.com/nolive-revision.json.gz"
LOCAL_TZ = ZoneInfo("Europe/Belgrade")
SPORTS = {1: "football", 2: "basketball", 6: "tennis", 4: "hockey", 3: "handball", 5: "volleyball", 19: "table_tennis"}
X12 = {"F1": ("1X2", "1"), "FX": ("1X2", "X"), "F2": ("1X2", "2")}
DC = {"DC1X": ("DC", "1X"), "DC12": ("DC", "12"), "DCX2": ("DC", "X2")}
CODES = {
    "football": {
        **X12, **DC,
        "o0_1": ("OU_1.5", "U"), "o2p": ("OU_1.5", "O"),
        "o0_2": ("OU_2.5", "U"), "o3p": ("OU_2.5", "O"),
        "o0m3": ("OU_3.5", "U"), "o4p": ("OU_3.5", "O"),
        "GG": ("BTTS", "GG"), "NG": ("BTTS", "NG"),
    },
    "basketball": {**X12, "F_OT_1": ("12_OT", "1"), "F_OT_2": ("12_OT", "2")},
    "tennis": {"F1": ("12", "1"), "F2": ("12", "2")},
    "hockey": {**X12, **DC},
    "handball": X12,
    "volleyball": {"F1": ("12", "1"), "F2": ("12", "2")},
    "table_tennis": {"F1": ("12", "1"), "F2": ("12", "2")},
}


class TopbetScraper(Scraper):
    name = "Topbet"

    def __init__(self) -> None:
        self.client = make_client({"Referer": "https://sportbook.topbetbo.com/sports"}, timeout=60)

    async def fetch(self) -> list[Event]:
        r = await self.client.get(OFFER_URL)
        r.raise_for_status()
        raw = r.content
        # ~11 MB of JSON: decode off the event loop so the bot stays responsive
        data = await asyncio.to_thread(lambda: json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw))

        now = datetime.now(timezone.utc)
        events = []
        for e in (data.get("events") or {}).values():
            sport = SPORTS.get(e.get("id_ct"))
            if not sport or not e.get("is_offer_active") or e.get("is_special") or e.get("id_status") != 1:
                continue
            start = datetime.fromisoformat(e["start_time"]).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
            if start <= now:
                continue
            br = (e.get("meta") or {}).get("id_external")
            ev = Event(
                bookie=self.name,
                event_id=str(e["id"]),
                sport=sport,
                home=e["participant_1"]["name"],
                away=e["participant_2"]["name"],
                start=start,
                betradar_id=str(br) if br else None,
                # the sportsbook's own match page, addressed by the match code
                url=f"https://sportbook.topbetbo.com/mobmatch/{e.get('code')}-false",
            )
            codes = CODES[sport]
            for key, o in (e.get("offer") or {}).items():
                if key in codes:
                    ev.add(*codes[key], (o or {}).get("value"))
            if ev.markets:
                events.append(ev)
        return events

    async def close(self) -> None:
        await self.client.aclose()
