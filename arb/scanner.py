from __future__ import annotations

import asyncio
import logging
import os
import pickle
import time
from dataclasses import dataclass, field

from arb.arbitrage import Arb, find_arbs
from arb.config import DATA_DIR
from arb.matcher import group_events
from arb.models import Event
from arb.scrapers import ALL_SCRAPERS

log = logging.getLogger(__name__)


@dataclass
class BookieResult:
    events: list[Event] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None


@dataclass
class ScanResult:
    bookies: dict[str, BookieResult]
    groups: list[list[Event]]
    arbs: list[Arb]
    regions: set[str] = field(default_factory=lambda: {"rs", "crypto"})


# Crypto books are scanned at most this often (their feeds are big and the odds
# move slowly); an arb's odds are still re-checked right before it is shown.
MIN_INTERVAL = {"crypto": 60}
CACHE_FILE = DATA_DIR / "last_odds.pkl"
CACHE_MAX_AGE = 15 * 60  # after a restart, crypto odds up to 15 min old are used until fresh ones arrive
VERIFY_TIMEOUT = 10  # re-checking an arb never makes the user wait longer than this
SCAN_WAIT = 100  # a scan doesn't wait longer than this for any bookie (the fetch goes on in the background)


def _match_and_find(events: list[Event], min_profit: float):
    groups = group_events(events)
    return groups, find_arbs(groups, min_profit)


class Scanner:
    """Keeps scraper instances alive between scans (sessions, tokens, browser)."""

    def __init__(self, scraper_classes=ALL_SCRAPERS) -> None:
        self.scrapers = [cls() for cls in scraper_classes]
        self._inflight: dict[str, asyncio.Task] = {}
        self._last: dict[str, tuple[float, BookieResult]] = {}  # last good fetch per bookie
        self._failed: dict[str, tuple[float, int, float]] = {}  # bookie -> (last failure, failures in a row, retry at)
        self._load_cache()

    # ---- crypto results survive a restart (rate-limited sites would start from zero)

    def _load_cache(self) -> None:
        try:
            with open(CACHE_FILE, "rb") as f:
                saved = pickle.load(f)
        except Exception:
            return
        now = time.time()
        names = {s.name for s in self.scrapers}
        for name, (at, events) in saved.items():
            if name in names and now - at < CACHE_MAX_AGE:
                for ev in events:
                    ev.__dict__.setdefault("depth", {})  # saved by an older version
                    ev.__dict__.setdefault("reversed", False)
                    ev.__dict__.setdefault("bet_ref", {})
                self._last[name] = (at, BookieResult(events, 0.0))

    def _save_cache(self) -> None:
        crypto = {s.name for s in self.scrapers if s.region == "crypto"}
        data = {n: (at, r.events) for n, (at, r) in self._last.items() if n in crypto}
        try:
            tmp = CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(data, f)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            log.warning("odds cache not saved: %s", e)

    def _backing_off(self, scraper) -> bool:
        """After a failure (e.g. 429 Too Many Requests) leave the site alone for a while:
        2, 4, 8 ... up to 15 minutes, instead of hammering it every scan."""
        failed = self._failed.get(scraper.name)
        if not failed:
            return False
        return time.time() < failed[2]

    async def _run(self, scraper, fresh: bool = False) -> BookieResult:
        """Single-flight per bookie: if a fetch is already running (scan loop vs. an
        odds check), wait for that one instead of hitting the site twice."""
        task = self._inflight.get(scraper.name)
        if task is None or task.done():
            task = asyncio.ensure_future(self._fetch(scraper, fresh))
            self._inflight[scraper.name] = task
        return await asyncio.shield(task)

    async def _fetch(self, scraper, fresh: bool) -> BookieResult:
        t = time.perf_counter()
        try:
            fetch = scraper.fetch_fresh if fresh else scraper.fetch
            events = await asyncio.wait_for(fetch(), timeout=getattr(scraper, "fetch_timeout", 120))
            res = BookieResult(events, time.perf_counter() - t)
            self._last[scraper.name] = (time.time(), res)
            self._failed.pop(scraper.name, None)
            return res
        except Exception as e:  # one broken bookie must not stop the scan
            msg = f"{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"
            log.warning("%s failed - %s", scraper.name, msg)
            if scraper.region == "crypto":
                count = self._failed.get(scraper.name, (0, 0, 0))[1] + 1
                wait = min(60 * 2 ** count, 900)
                # the site says how long to stay away (Cloudflare 1015 + Retry-After): respect it,
                # every request before that only extends the block
                retry = getattr(getattr(e, "response", None), "headers", {}).get("Retry-After")
                if retry and str(retry).isdigit():
                    wait = max(wait, int(retry) + 30)
                    log.warning("%s: site asks to wait %s s", scraper.name, retry)
                self._failed[scraper.name] = (time.time(), count, time.time() + wait)
            return BookieResult([], time.perf_counter() - t, msg)

    async def fetch_bookies(self, wanted: dict[str, list[str]],
                            timeout: float = VERIFY_TIMEOUT) -> dict[str, BookieResult]:
        """Fresh odds from just these bookies ({bookie: [event ids]}), used to re-check
        an arb before it is shown. Per-event where the site allows it, else the whole offer.
        Bookies that don't answer within `timeout` are left out (the fetch keeps running
        in the background and feeds the next scan) - the caller keeps their scan odds."""
        chosen = [s for s in self.scrapers if s.name in wanted]
        tasks = {asyncio.ensure_future(self._run_events(s, wanted[s.name])): s for s in chosen}
        if not tasks:
            return {}
        done, _ = await asyncio.wait(tasks, timeout=timeout)
        return {tasks[t].name: t.result() for t in done}

    async def _run_events(self, scraper, event_ids: list[str]) -> BookieResult:
        t = time.perf_counter()
        try:
            events = await asyncio.wait_for(scraper.fetch_events(event_ids), timeout=30)
        except Exception as e:
            log.warning("%s per-event refresh failed - %s", scraper.name, e)
            events = None
        if events is None:
            return await self._run(scraper, fresh=True)
        return BookieResult(events, time.perf_counter() - t)

    async def _scan_one(self, scraper) -> BookieResult:
        last = self._last.get(scraper.name)
        min_interval = MIN_INTERVAL.get(scraper.region, 0)
        if self._backing_off(scraper):
            return last[1] if last else BookieResult([], 0.0, "pauza posle greške (sajt ograničava zahteve)")
        if last and min_interval:
            # crypto: never make the scan wait for a slow site (Shuffle ~2 min) - use the
            # last result and refresh in the background for the next scan
            if time.time() - last[0] >= min_interval:
                task = self._inflight.get(scraper.name)
                if task is None or task.done():
                    self._inflight[scraper.name] = asyncio.ensure_future(self._fetch(scraper, False))
            return last[1]
        return await self._run(scraper)

    async def scan(self, min_profit: float = 0.0, regions: set[str] | None = None) -> ScanResult:
        """Scan the bookies of these regions ("rs" / "crypto"; None = all)."""
        active = [s for s in self.scrapers if regions is None or s.region in regions]
        tasks = [asyncio.ensure_future(self._scan_one(s)) for s in active]
        await asyncio.wait(tasks, timeout=SCAN_WAIT)
        results = []
        for s, t in zip(active, tasks):
            if t.done():
                results.append(t.result())
            else:  # still loading (first start of a slow site): the fetch continues, next scan picks it up
                t.cancel()
                results.append(BookieResult([], SCAN_WAIT, "još se učitava"))
        bookies = {s.name: r for s, r in zip(active, results)}
        events = [ev for r in results for ev in r.events]
        # CPU work in a worker thread so the bot's event loop keeps answering
        groups, arbs = await asyncio.to_thread(_match_and_find, events, min_profit)
        await asyncio.to_thread(self._save_cache)
        return ScanResult(bookies, groups, arbs, set(regions) if regions else {s.region for s in self.scrapers})

    async def close(self) -> None:
        for s in self.scrapers:
            try:
                await s.close()
            except Exception:
                pass
