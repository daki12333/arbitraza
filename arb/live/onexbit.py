"""1xBit executor. 1xBit has no public betting API, so the bot bets the way the site does:

1. 🔵 Prijava: the bot opens a real browser window (Edge, its own profile in
   data/1xbit_profile) and the user logs in there by hand - captcha / 2FA included. The bot
   never sees the password; the session stays in that profile.
2. 🎓 Nauči: the user places ONE small pre-match bet by hand in that window. The bot reads
   the request the site sends for it (address, headers, JSON body) and keeps it as a
   template in the Windows Credential Manager (arb.live.vault) - it holds the session data.
3. Every automatic bet is that same request, sent from inside that same logged-in page,
   with only the game, the pick, the odd and the stake changed (Event.bet_ref from the scraper).

Before teaching, set on 1xBit: odds changes → "never accept" (don't accept changed odds),
so a moved odd is refused instead of taken at a worse price. The account must be in USDT
(stakes are in $)."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time

from arb.config import DATA_DIR
from arb.live import Fill, vault

log = logging.getLogger(__name__)

HOME = "https://1xbit.com/en"
PROFILE_DIR = DATA_DIR / "1xbit_profile"
TEMPLATE = "1xbit_bet_template"
PLACE_TIMEOUT = 20  # s

# key names the bet request uses (any case)
GAME_KEYS = ("gameid", "game_id")
TYPE_KEYS = ("type", "t")
COEF_KEYS = ("coef", "coefficient", "c", "odd", "odds")
PARAM_KEYS = ("param", "p")
STAKE_KEYS = ("summ", "sum", "amount", "stake")
# set by the browser itself, can't (and mustn't) be copied into fetch()
SKIP_HEADERS = ("cookie", "content-length", "host", "origin", "referer", "user-agent", "connection",
                "accept-encoding", "te", "priority")


def _key(d: dict, names: tuple[str, ...]) -> str | None:
    low = {k.lower(): k for k in d}
    return next((low[n] for n in names if n in low), None)


def _events_key(body: dict) -> str | None:
    """The list of picks on the slip: dicts with a game id and an odd."""
    for k, v in body.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and _key(v[0], GAME_KEYS) and _key(v[0], COEF_KEYS):
            return k
    return None


def looks_like_bet(body) -> bool:
    """A placed bet: a slip with a pick and a stake above 0 (not the 'check the slip' calls)."""
    if not isinstance(body, dict):
        return False
    ek, sk = _events_key(body), _key(body, STAKE_KEYS)
    if not ek or not sk:
        return False
    try:
        return float(body[sk]) > 0
    except (TypeError, ValueError):
        return False


def make_template(url: str, headers: dict, body: dict) -> dict:
    ek = _events_key(body)
    ev = body[ek][0]
    return {
        "url": url,
        "headers": {k: v for k, v in headers.items()
                    if not k.startswith((":", "sec-", "proxy-")) and k.lower() not in SKIP_HEADERS},
        "body": body,
        "events_key": ek,
        "keys": {"game": _key(ev, GAME_KEYS), "type": _key(ev, TYPE_KEYS), "coef": _key(ev, COEF_KEYS),
                 "param": _key(ev, PARAM_KEYS), "stake": _key(body, STAKE_KEYS)},
        "learned_at": time.time(),
    }


def _same_type(old, value: float):
    """Write `value` the way the site wrote the old one ("1.5" stays a string)."""
    if isinstance(old, str):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(old, int) and float(value).is_integer():
        return int(value)
    return value


def build_body(t: dict, ref: dict, odd: float, stake: float) -> dict:
    """The learned request with this pick, odd and stake."""
    body = copy.deepcopy(t["body"])
    keys = t["keys"]
    ev = copy.deepcopy(body[t["events_key"]][0])
    ev[keys["game"]] = ref["GameId"]
    if keys["type"]:
        ev[keys["type"]] = ref["Type"]
    ev[keys["coef"]] = _same_type(ev[keys["coef"]], round(odd, 3))
    if keys["param"]:
        ev[keys["param"]] = ref.get("Param") or 0
    body[t["events_key"]] = [ev]
    body[keys["stake"]] = _same_type(body[keys["stake"]], round(stake, 2))
    return body


def _get(d: dict, *names):
    low = {k.lower(): v for k, v in d.items()}
    return next((low[n] for n in names if n in low), None)


def parse_response(status: int, text: str) -> tuple[bool, str, float | None, str]:
    """(accepted, ticket id, odd it was taken at, error) from the site's answer."""
    try:
        data = json.loads(text)
    except ValueError:
        return False, "", None, f"HTTP {status}: {text[:150]}"
    if not isinstance(data, dict):
        return False, "", None, f"HTTP {status}: {str(data)[:150]}"
    value = _get(data, "value", "data", "result")
    value = value if isinstance(value, dict) else {}
    error = _get(data, "error", "errormsg", "message")
    success = _get(data, "success")
    ticket = _get(value, "id", "betguid", "couponid", "betid") or _get(data, "id", "betid")
    ok = status < 400 and (success is True or (success is None and ticket is not None)) and not (
        error and success is not True)
    coef = _get(value, "coef", "coefficient")
    try:
        coef = float(coef) if coef is not None else None
    except (TypeError, ValueError):
        coef = None
    return ok, str(ticket or ""), coef, "" if ok else str(error or f"HTTP {status}: {text[:150]}")


_FETCH_JS = """async ({url, headers, body}) => {
  const r = await fetch(url, {method: "POST", headers, body, credentials: "include"});
  return {status: r.status, text: await r.text()};
}"""


class OneXBitExecutor:
    name = "1xBit"

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self.teaching = False

    # ---- the learned request

    @staticmethod
    def template() -> dict | None:
        try:
            raw = vault.get(TEMPLATE)
        except vault.VaultError:
            return None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    @staticmethod
    def forget() -> None:
        try:
            vault.delete(TEMPLATE)
        except vault.VaultError:
            pass

    # ---- the browser

    @property
    def window_open(self) -> bool:
        return self._ctx is not None

    async def _page(self):
        if self._ctx is None:
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            kwargs = {"headless": os.getenv("ONEXBIT_HEADLESS") == "1", "no_viewport": True}
            channel = os.getenv("ONEXBIT_BROWSER", os.getenv("MOZZART_BROWSER", "msedge"))
            if channel != "chromium":
                kwargs["channel"] = channel
            self._ctx = await self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
            self._ctx.on("close", lambda *_: self._closed())
        page = next((p for p in self._ctx.pages if not p.is_closed() and "1xbit" in p.url), None)
        if page is None:
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            await page.goto(HOME, wait_until="domcontentloaded", timeout=60_000)
        return page

    def _closed(self) -> None:
        self._ctx = None

    async def open_window(self) -> str:
        """🔵 Prijava: show the 1xBit window, the user logs in there."""
        try:
            page = await self._page()
            await page.bring_to_front()
        except Exception as e:
            log.warning("1xBit window: %s", e)
            return f"❌ Ne mogu da otvorim prozor: {str(e).splitlines()[0][:150]}"
        return "ok"

    async def teach(self, timeout: float = 600) -> tuple[bool, str]:
        """🎓 Wait for the user's own bet in the window and learn its request."""
        page = await self._page()
        await page.bring_to_front()
        loop = asyncio.get_running_loop()
        found: asyncio.Future = loop.create_future()

        def on_request(req) -> None:
            if req.method != "POST" or found.done():
                return
            try:
                body = req.post_data_json
            except Exception:
                return
            if looks_like_bet(body):
                found.set_result((req, body))

        self.teaching = True
        self._ctx.on("request", on_request)
        try:
            req, body = await asyncio.wait_for(found, timeout)
            headers = await req.all_headers()
            resp = await req.response()
            text = await resp.text() if resp else ""
            status = resp.status if resp else 0
        except asyncio.TimeoutError:
            return False, "nisam video nijednu uplatu u prozoru"
        finally:
            self.teaching = False
            try:
                self._ctx.remove_listener("request", on_request)
            except Exception:
                pass
        ok, ticket, coef, err = parse_response(status, text)
        t = make_template(req.url, headers, body)
        vault.put(TEMPLATE, json.dumps(t))
        keys = t["keys"]
        ev = body[t["events_key"]][0]
        info = (f"adresa {req.url.split('?')[0]}, utakmica {ev.get(keys['game'])}, kvota {ev.get(keys['coef'])}, "
                f"ulog {body.get(keys['stake'])}")
        if not ok:
            return True, f"naučeno ({info}), ali sajt je tu uplatu odbio: {err}"
        return True, f"naučeno ({info}), tiket {ticket or '?'}"

    async def place(self, ref: dict, odd: float, stake: float, dry: bool = False) -> Fill:
        t = self.template()
        if not t:
            return Fill(False, error="1xBit uplata nije naučena (🎓 Nauči)")
        if not ref or "GameId" not in ref:
            return Fill(False, error="nema podataka o ovom ishodu na 1xBit-u")
        body = build_body(t, ref, odd, stake)
        if dry:
            return Fill(True, "proba", stake, stake * odd)
        async with self._lock:
            try:
                page = await self._page()
                res = await asyncio.wait_for(page.evaluate(_FETCH_JS, {
                    "url": t["url"], "headers": t["headers"] | {"content-type": "application/json"},
                    "body": json.dumps(body, separators=(",", ":"))}), PLACE_TIMEOUT)
            except asyncio.TimeoutError:
                return Fill(False, stake=stake, unknown=True,
                            error="1xBit se nije javio na vreme – PROVERI na sajtu da li je tiket ipak prošao")
            except Exception as e:
                return Fill(False, error=f"prozor 1xBit-a: {str(e).splitlines()[0][:150]}")
        ok, ticket, coef, err = parse_response(res["status"], res["text"])
        log.info("1xBit bet %s: %s %s %s", ref.get("GameId"), ok, ticket, err)
        if not ok:
            return Fill(False, error=err)
        taken = coef or odd
        return Fill(True, ticket, stake, stake * taken)

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = None
