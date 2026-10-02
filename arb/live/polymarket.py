"""Polymarket executor through the official CLOB API (py-clob-client).

Connect once in the bot (🤖 Auto → 🟣 Poveži Polymarket): private key + the address of the
Polymarket account (funder) + account type (1 = email/Google, 2 = MetaMask). The key goes to
the Windows Credential Manager (arb.live.vault), the rest to data/users.json.

Every buy is a market order "FOK" (fill or kill): all of it at the worst price we allow,
or nothing. The worst price is where the whole arb is at zero, fee included."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time

import httpx

from arb.live import Fill, vault

log = logging.getLogger(__name__)

HOST = "https://clob.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"
CHAIN_ID = 137  # Polygon
KEY_NAME = "polymarket_private_key"
MIN_ORDER = 1.0  # $ - Polymarket doesn't take smaller market buys
BALANCE_TTL = 60  # s - balance read from the API is reused this long

KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def cost(price: float, rate: float) -> float:
    """$ per share incl. the taker fee (rate * p * (1 - p)) - same model as the scraper."""
    return price + rate * price * (1 - price)


def worst_price(stake: float, need: float, rate: float, tick: str | float = "0.01") -> float | None:
    """Highest price per share at which `stake` still returns at least `need` when it wins
    (fee included), rounded DOWN to the market's tick. None = no valid price (need too high)."""
    if stake <= 0 or need <= 0:
        return None
    c = stake / need  # allowed cost per share
    if rate > 0:
        disc = (1 + rate) ** 2 - 4 * rate * c
        if disc < 0:
            return None
        p = ((1 + rate) - math.sqrt(disc)) / (2 * rate)
    else:
        p = c
    return on_tick(p, tick)


def on_tick(p: float, tick: str | float) -> float | None:
    """`p` rounded DOWN to the tick (never a worse price than allowed); None if outside (tick, 1 - tick)."""
    t = float(tick)
    decimals = max(0, -int(math.floor(math.log10(t))))
    p = round(math.floor(p / t + 1e-9) * t, decimals)
    return p if t <= p <= 1 - t else None


def normalize_key(key: str) -> str | None:
    key = key.strip()
    if not KEY_RE.match(key):
        return None
    return key if key.startswith("0x") else "0x" + key


class PolyExecutor:
    name = "Polymarket"

    def __init__(self, client_factory=None) -> None:
        self._client = None
        self._factory = client_factory or _make_client
        self.funder = ""
        self.sig = 1
        self.error = ""
        self._balance: tuple[float, float] | None = None  # (at, $)
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None

    def has_key(self) -> bool:
        try:
            return bool(vault.get(KEY_NAME))
        except vault.VaultError:
            return False

    async def connect(self, funder: str, sig: int, key: str | None = None) -> tuple[bool, str]:
        """Log in with the stored key (or `key`, which is then stored). (ok, message)."""
        try:
            if key:
                key = normalize_key(key)
                if not key:
                    return False, "to nije privatni ključ (64 heksa znaka, sa ili bez 0x)"
            stored = key or vault.get(KEY_NAME)
        except vault.VaultError as e:
            return False, str(e)
        if not stored:
            return False, "privatni ključ nije unet"
        if not ADDRESS_RE.match(funder or ""):
            return False, "adresa Polymarket naloga nije dobra (0x + 40 znakova)"
        try:
            client = await asyncio.to_thread(self._factory, stored, funder, sig)
        except ImportError:
            return False, "nije instalirana biblioteka py-clob-client (pokreni start.bat ponovo)"
        except Exception as e:
            log.warning("Polymarket connect failed: %s", e)
            return False, f"Polymarket ne prihvata prijavu: {_err(e)}"
        if key:
            try:
                vault.put(KEY_NAME, key)
            except vault.VaultError as e:
                return False, str(e)
        self._client, self.funder, self.sig, self.error = client, funder, sig, ""
        self._balance = None
        bal = await self.balance(force=True)
        return True, "povezano" + (f", balans {bal:.2f} $" if bal is not None else "")

    def forget(self) -> None:
        self._client = None
        self._balance = None
        try:
            vault.delete(KEY_NAME)
        except vault.VaultError:
            pass

    async def balance(self, force: bool = False) -> float | None:
        """USDC on the account ($), from the API (cached BALANCE_TTL s)."""
        if not self._client:
            return None
        if not force and self._balance and time.time() - self._balance[0] < BALANCE_TTL:
            return self._balance[1]
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            r = await asyncio.to_thread(self._client.get_balance_allowance,
                                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            value = int(r.get("balance") or 0) / 1e6
        except Exception as e:
            self.error = _err(e)
            log.warning("Polymarket balance failed: %s", e)
            return self._balance[1] if self._balance else None
        self._balance = (time.time(), value)
        return value

    @staticmethod
    async def geoblock() -> tuple[bool | None, str]:
        """(blocked, country). None = couldn't check - then nothing is bet."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(GEOBLOCK)
                data = r.json()
            return bool(data.get("blocked")), str(data.get("country") or "?")
        except Exception as e:
            log.warning("geoblock check failed: %s", e)
            return None, ""

    async def buy(self, token: str, amount: float, worst: float, tick: str, neg_risk: bool, rate: float,
                  dry: bool = False) -> Fill:
        """Spend `amount` $ on this outcome token, at prices up to `worst` - all of it or nothing (FOK)."""
        if not self._client:
            return Fill(False, error="Polymarket nije povezan")
        amount = math.floor(amount * 100) / 100  # the API takes cents
        if amount < MIN_ORDER:
            return Fill(False, error=f"Polymarket ne prima manje od {MIN_ORDER:g} $")
        async with self._lock:
            try:
                from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

                # the market's tick right now (it gets finer near 0 / 1): the worst price goes DOWN onto it
                tick = str(await asyncio.to_thread(self._client.get_tick_size, token) or tick)
                worst = on_tick(worst, tick)
                if worst is None:
                    return Fill(False, error="nema cene na kojoj se ovo isplati")
                args = MarketOrderArgs(token_id=token, amount=amount, side="BUY", price=worst,
                                       order_type=OrderType.FOK)
                opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk or None)
                order = await asyncio.to_thread(self._client.create_market_order, args, opts)
                if dry:  # signed, but not sent
                    return Fill(True, "proba", amount, amount / cost(worst, rate), worst)
                resp = await asyncio.to_thread(self._client.post_order, order, OrderType.FOK)
            except Exception as e:
                return Fill(False, stake=amount, error=_err(e))
        self._balance = None  # changed
        return parse_order(resp, amount, worst, rate)


def parse_order(resp, amount: float, worst: float, rate: float) -> Fill:
    """post_order() answer -> Fill. Matched FOK: {"success": true, "orderID": ..., "status": "matched",
    "makingAmount": "$ spent", "takingAmount": "shares"}."""
    if not isinstance(resp, dict):
        return Fill(False, stake=amount, error=str(resp)[:200])
    if not resp.get("success", False) or resp.get("errorMsg"):
        return Fill(False, stake=amount, error=str(resp.get("errorMsg") or resp)[:200])
    status = str(resp.get("status") or "").lower()
    if status not in ("matched", "mined", "confirmed", ""):
        return Fill(False, str(resp.get("orderID") or ""), amount, error=f"nalog nije izvršen ({status})")
    try:
        spent = float(resp.get("makingAmount") or 0) or amount
        shares = float(resp.get("takingAmount") or 0)
    except (TypeError, ValueError):
        spent, shares = amount, 0.0
    price = spent / shares if shares else worst
    payout = spent / cost(price, rate)  # fee off the shares
    return Fill(True, str(resp.get("orderID") or ""), spent, payout, price)


def _make_client(key: str, funder: str, sig: int):
    from py_clob_client.client import ClobClient

    client = ClobClient(HOST, chain_id=CHAIN_ID, key=key, signature_type=sig, funder=funder)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _err(e: Exception) -> str:
    msg = getattr(e, "error_msg", None) or str(e) or type(e).__name__
    if isinstance(msg, dict):
        msg = msg.get("error") or msg.get("errorMsg") or msg
    return str(msg)[:200]


async def resolution(condition: str, token: str) -> bool | None:
    """Did this outcome token win? None = market not resolved yet (or no answer)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{HOST}/markets/{condition}")
            m = r.json()
    except Exception as e:
        log.warning("Polymarket market %s: %s", condition, e)
        return None
    tokens = m.get("tokens") or []
    if not m.get("closed") or not any(t.get("winner") for t in tokens):
        return None
    return any(str(t.get("token_id")) == str(token) and t.get("winner") for t in tokens)
