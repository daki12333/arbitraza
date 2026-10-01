from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod

import httpx

from arb.models import Event

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def slugify(s: str) -> str:
    s = s.replace("đ", "dj").replace("Đ", "Dj")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


_ssl = None


def _ssl_context():
    """One certificate store for every client: building it costs ~0.6 s, x 30 bookies at start."""
    global _ssl
    if _ssl is None:
        _ssl = httpx.create_ssl_context()
    return _ssl


def make_client(headers: dict | None = None, timeout: float = 30) -> httpx.AsyncClient:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    return httpx.AsyncClient(headers=h, timeout=timeout, follow_redirects=True, verify=_ssl_context())


class Scraper(ABC):
    """One bookmaker. fetch() returns upcoming (pre-match) football events."""

    name: str = "base"
    region: str = "rs"  # "rs" = Serbian licensed bookie, "crypto" = crypto sportsbook

    @abstractmethod
    async def fetch(self) -> list[Event]: ...

    async def fetch_fresh(self) -> list[Event]:
        """Like fetch(), but never from a cache (used to re-check an arb's odds)."""
        return await self.fetch()

    async def fetch_events(self, event_ids: list[str]) -> list[Event] | None:
        """Fresh odds for just these events, if the site allows it cheaply.
        None = not supported, the caller falls back to fetch_fresh()."""
        return None

    async def close(self) -> None:
        pass
