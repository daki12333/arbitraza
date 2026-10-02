"""Runs the scan loop and answers "what are the arbs for this user right now"."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Awaitable, Callable

from arb.arbitrage import Arb, find_arbs
from arb.matcher import orient
from arb.middles import find_middles
from arb.models import Event
from arb.scanner import Scanner, ScanResult
from arb.tg.storage import UserSettings

log = logging.getLogger(__name__)

SCAN_TIMEOUT = 240  # a stuck scraper must not freeze the loop forever


def group_key(group: list[Event]) -> str:
    """Same for the full group and for any user's filtered copy of it."""
    ev = group[0]
    return ev.group or ev.betradar_id or f"{ev.bookie[:2]}{ev.event_id}"


def arb_key(arb: Arb) -> str:
    return f"{group_key(arb.events)}:{arb.market}"


# Telegram takes at most 64 bytes of callback data, and some group keys (a bookie's own
# long event id) plus the market don't fit: buttons carry a short token instead.
_cb_keys: dict[str, str] = {}
CB_KEYS_MAX = 50_000


def cb_key(key: str) -> str:
    """The arb key as it goes into callback data: 12 hex chars, no ':'."""
    token = hashlib.sha1(key.encode()).hexdigest()[:12]
    if token not in _cb_keys:
        if len(_cb_keys) >= CB_KEYS_MAX:
            del _cb_keys[next(iter(_cb_keys))]  # forget the oldest
        _cb_keys[token] = key
    return token


def key_from_cb(token: str) -> str:
    """Back from cb_key(). Buttons from before a restart (or the old "group:market" form)
    come back as they are - lookup() then just doesn't find the arb."""
    return _cb_keys.get(token, token)


def _legs_sig(arb: Arb) -> tuple:
    return tuple((l.bookie, l.market, l.outcome, l.odd) for l in arb.legs)


RECHECK_MEMORY = 600  # s - how long a re-check's result overrides scans that still carry the older odds


class ArbService:
    def __init__(self, interval: int) -> None:
        self.interval = interval
        self.scanner = Scanner()
        self.result: ScanResult | None = None
        self.scanned_at = 0.0
        self.groups: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()
        self._arbs_cache: dict[tuple[str, ...], list[Arb]] = {}
        self._middles_cache: dict[tuple[str, ...], list] = {}
        # re-checked arbs: key -> (when, legs of the outdated versions, the fresh arb or None = gone).
        # Lists show the fresh version (new %) or drop a gone one right away - and after the
        # next scans too, while slower sites still return those same outdated odds
        self._rechecked: dict[str, tuple[float, set[tuple], Arb | None]] = {}
        self.on_scan: Callable[[], Awaitable[None]] | None = None
        # which regions someone is using right now ({"rs"}, {"crypto"} or both);
        # only those bookies are scanned
        self.regions: Callable[[], set[str]] = lambda: {"rs", "crypto"}

    @property
    def age(self) -> float:
        return time.time() - self.scanned_at if self.scanned_at else float("inf")

    async def scan(self) -> ScanResult:
        async with self._lock:
            t = time.perf_counter()
            res = await self.scanner.scan(regions=self.regions() or {"rs"})
            self.result, self.scanned_at = res, time.time()
            self.groups = {group_key(g): g for g in res.groups}
            self._arbs_cache = {}
            self._middles_cache = {}
            log.info("scan: %d groups, %d arbs in %.1fs", len(res.groups), len(res.arbs), time.perf_counter() - t)
            return res

    def covers(self, s: UserSettings | None) -> bool:
        """Does the last scan include this user's bookies (rs / crypto)?"""
        return self.result is not None and (s is None or s.mode in self.result.regions)

    async def fresh(self, s: UserSettings | None = None) -> ScanResult:
        """Latest result right away. Only waits when there is no scan of this user's
        region yet (first start, or right after switching rs <-> crypto) - the
        background loop keeps results fresh, users never wait for a rescan."""
        if not self.covers(s):
            if self._lock.locked():
                async with self._lock:
                    pass
            if not self.covers(s):
                await self.scan()
        return self.result

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.scan(), timeout=SCAN_TIMEOUT)
                if self.on_scan:
                    await self.on_scan()
            except Exception:
                log.exception("scan loop error")
            await asyncio.sleep(self.interval)

    # ---- per-user views -------------------------------------------------

    @staticmethod
    def _filter(group: list[Event], s: UserSettings) -> list[Event]:
        return [e for e in group if e.bookie in s.bookies]

    def middles_for(self, s: UserSettings) -> list:
        """Middles among this user's bookies (see arb.middles), once per scan."""
        if not self.result:
            return []
        key = tuple(s.bookies)
        if key not in self._middles_cache:
            self._middles_cache[key] = find_middles([self._filter(g, s) for g in self.result.groups])
        return self._middles_cache[key]

    def arbs_for(self, s: UserSettings) -> list[Arb]:
        """Computed once per scan per bookie selection (list, notifier and every
        button press share it)."""
        if not self.result:
            return []
        key = tuple(s.bookies)
        if key not in self._arbs_cache:
            if set(key) >= set(self.result.bookies):
                self._arbs_cache[key] = self.result.arbs
            else:
                self._arbs_cache[key] = find_arbs([self._filter(g, s) for g in self.result.groups])
        arbs = self._arbs_cache[key]
        if not self._rechecked:
            return arbs
        now = time.time()
        self._rechecked = {k: v for k, v in self._rechecked.items() if now - v[0] < RECHECK_MEMORY}
        out = []
        for a in arbs:
            r = self._rechecked.get(arb_key(a))
            if r is None or _legs_sig(a) not in r[1]:
                out.append(a)  # nothing newer than this scan
            elif r[2] is None:
                continue  # the re-check found it gone
            elif {l.bookie for l in r[2].legs} <= set(key):
                out.append(r[2])  # the odds just re-checked (new %)
            else:
                out.append(a)  # re-checked with bookies this user has switched off
        return out

    async def verify(self, arbs: list[Arb], s: UserSettings) -> list[Arb]:
        """Re-fetch the odds of every bookie these arbs use and recompute them.
        Returns only the arbs that still exist, with checked_at set - so what the
        user gets is minutes fresher than the scan (table tennis odds move fast)."""
        if not arbs or not self.result:
            return []
        needed: dict[str, list[str]] = {}
        for a in arbs:
            for leg in a.legs:
                ev = a.event_for(leg.bookie)
                if ev and ev.event_id not in needed.setdefault(leg.bookie, []):
                    needed[leg.bookie].append(ev.event_id)
        fresh = await self.scanner.fetch_bookies(needed)
        now = time.time()
        by_id = {(name, ev.event_id): ev for name, r in fresh.items() for ev in r.events}
        refreshed = {name for name, r in fresh.items() if not r.error}  # slow/failed ones keep scan odds
        out = []
        for arb in arbs:
            key = group_key(arb.events)
            group = self.groups.get(key)
            if not group:
                continue
            new_group, new_events = [], []
            for ev in group:
                if ev.bookie not in refreshed:
                    new_group.append(ev)  # not part of this arb: keep scan odds
                elif (ev.bookie, ev.event_id) in by_id:
                    new = by_id[(ev.bookie, ev.event_id)]
                    new.group = ev.group
                    new_group.append(new)
                    new_events.append(new)
                # refreshed but gone -> the match was taken off the offer
            if not new_group:
                continue
            orient(new_group[0], [e for e in new_events if e is not new_group[0]])
            self.groups[key] = new_group
            for fresh_arb in find_arbs([self._filter(new_group, s)]):
                if fresh_arb.market == arb.market:
                    # "just checked" only if every leg's bookie really answered in time
                    if all(l.bookie in refreshed for l in fresh_arb.legs):
                        fresh_arb.checked_at = now
                    fresh_arb.fresh = {l.bookie for l in fresh_arb.legs if l.bookie in refreshed}
                    fresh_arb.prev_odds = {(l.market, l.outcome): l.odd for l in arb.legs}
                    out.append(fresh_arb)
        found = {arb_key(a): a for a in out}
        for arb in arbs:
            k = arb_key(arb)
            fresh_arb = found.get(k)
            prev = self._rechecked.get(k)
            outdated = (prev[1] if prev else set()) | {_legs_sig(arb)}
            if fresh_arb is not None:
                outdated.discard(_legs_sig(fresh_arb))
            self._rechecked[k] = (now, outdated, fresh_arb)
            if fresh_arb is None:
                log.info("re-check: %s gone (%s)", k,
                         ", ".join(f"{l.bookie} {l.market} {l.outcome} {l.odd}" for l in arb.legs))
        out.sort(key=lambda a: a.profit_pct, reverse=True)
        return out

    async def lookup_fresh(self, key: str, s: UserSettings) -> Arb | None:
        """lookup() with the odds re-fetched from the bookies right now."""
        arb = self.lookup(key, s)
        if not arb:
            return None
        fresh = await self.verify([arb], s)
        return fresh[0] if fresh else None

    def lookup(self, key: str, s: UserSettings) -> Arb | None:
        """Current version of one arb (odds may have moved since it was sent)."""
        gkey, _, market = key.rpartition(":")
        group = self.groups.get(gkey)
        if not group:
            return None
        for arb in find_arbs([self._filter(group, s)]):
            if arb.market == market:
                return arb
        return None

    async def close(self) -> None:
        await self.scanner.close()
