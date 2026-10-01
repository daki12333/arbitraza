"""One shared headless Edge for the scrapers that need a real browser (Cloudflare,
tokens in the page). Each scraper gets its own tab/context in it, instead of every
scraper starting its own Edge process."""
from __future__ import annotations

import asyncio
import os

_lock = asyncio.Lock()
_pw = None
_browser = None


async def shared_browser():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            channel = os.getenv("MOZZART_BROWSER", "msedge")
            kwargs = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if channel != "chromium":
                kwargs["channel"] = channel
            _browser = await _pw.chromium.launch(**kwargs)
        return _browser


class BrowserPage:
    """A tab on `home_url` of the shared browser; API calls run inside it with fetch()."""

    home_url = ""
    wait_ms = 4000

    def __init__(self) -> None:
        self._page = None

    async def page(self):
        if self._page and not self._page.is_closed():
            return self._page
        browser = await shared_browser()
        ver = browser.version
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
        if os.getenv("MOZZART_BROWSER", "msedge") == "msedge":
            ua += f" Edg/{ver}"
        ctx = await browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 800})
        self._page = await ctx.new_page()
        await self._page.goto(self.home_url, wait_until="domcontentloaded", timeout=60_000)
        await self._page.wait_for_timeout(self.wait_ms)
        return self._page

    async def reset(self) -> None:
        if self._page:
            try:
                await self._page.context.close()
            except Exception:
                pass
        self._page = None
