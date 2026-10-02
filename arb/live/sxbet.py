"""SX Bet executor through the V3 order book API (api.sx.bet).

A taker bet is an order like any other, sent with timeInForce "FOK" (fill or kill): all of the
stake at the given odds or better, or nothing. The order is signed (EIP-712, struct "Order")
with the wallet's private key; the domain, the USDC token and the odds ladder come from
GET /metadata/obv3. Every private call also carries the API key (x-sx-api-key) - the same one
the scraper uses (🏦 Kladionice → 🔑 SX Bet ključ).

Connect once in the bot (/bot → 🔵 Poveži SX Bet): the private key of the wallet the API key
belongs to. The key goes to the Windows Credential Manager (arb.live.vault), never to a file.
The money has to sit in the account's V3 trading balance (deposit on sx.bet first)."""
from __future__ import annotations

import asyncio
import logging
import math
import secrets as pyrandom
import time

import httpx

from arb import secrets
from arb.live import Fill, vault
from arb.live.polymarket import normalize_key
from arb.scrapers.sxbet import FEE

log = logging.getLogger(__name__)

API = "https://api.sx.bet"
KEY_NAME = "sxbet_private_key"
MIN_ORDER = 1.0  # $ - /metadata/obv3 orderSizeMinimum (1 USDC)
BALANCE_TTL = 60  # s
ORDER_EXPIRY = 3600  # s - a FOK order is matched at once; the expiry only has to be in the future
LADDER_DEFAULT = 125  # oddsLadderStepSize in 1e-5 units of probability (125 -> 0.125 %)
CHECKS_AFTER_TIMEOUT = 3  # GET /orders-v3/{id} this many times when matching didn't answer in time

ORDER_TYPES = {"Order": [
    {"name": "marketHash", "type": "bytes32"},
    {"name": "baseToken", "type": "address"},
    {"name": "totalBetSize", "type": "uint256"},
    {"name": "percentageOdds", "type": "uint256"},
    {"name": "salt", "type": "uint256"},
    {"name": "expiry", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "isMakerBettingOutcomeOne", "type": "bool"},
]}


def max_prob(min_odd: float, step: int = LADDER_DEFAULT) -> int | None:
    """The worst price we accept as SX percentageOdds: the implied probability whose odds after
    SX's fee are still `min_odd`, put onto the odds ladder - DOWN (never a worse price), except
    when a ladder step lies within the scraper's rounding above: it rounds odds to 3 decimals
    (±0.0005), so the best offer itself can sit a hair above. None = no valid price."""
    if min_odd <= 1:
        return None
    p = 1 / (1 + (min_odd - 1) / (1 - FEE))  # inverse of scrapers.sxbet._odd
    exact = p * 100_000  # probability in 1e-5 units
    slack = 0.0005 * p * p / (1 - FEE) * 100_000 + 0.5  # what ±0.0005 in odds moves the probability
    up = math.ceil(exact / step - 1e-9) * step
    units = up if up - exact <= slack else math.floor(exact / step + 1e-9) * step
    if units <= 0 or units >= 100_000:
        return None
    return units * 10 ** 15  # 1e-5 units -> 1e20 units


def odd_of(percentage_odds: int) -> float:
    """Decimal odds after the fee for a taker percentageOdds (same as the scraper)."""
    p = percentage_odds / 1e20
    return 1 + (1 / p - 1) * (1 - FEE)


def sign_order(order: dict, domain: dict, key: str) -> str:
    """EIP-712 signature (0x + r s v) of the 8-field V3 order."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    message = {k: order[k] for k in ("marketHash", "baseToken", "totalBetSize", "percentageOdds", "salt", "expiry",
                                     "maker", "isMakerBettingOutcomeOne")}
    signable = encode_typed_data(domain_data=domain, message_types=ORDER_TYPES, message_data=message)
    sig = Account.sign_message(signable, private_key=key).signature
    return "0x" + bytes(sig).hex().removeprefix("0x")


def address_of(key: str) -> str:
    from eth_account import Account

    return Account.from_key(key).address


class SXExecutor:
    name = "SX Bet"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=30)
        self.address = ""
        self.error = ""
        self._key: str | None = None
        self._meta: dict | None = None
        self._balance: tuple[float, float] | None = None  # (at, $)
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self._key and self._meta)

    def has_key(self) -> bool:
        try:
            return bool(vault.get(KEY_NAME))
        except vault.VaultError:
            return False

    @staticmethod
    def _headers() -> dict:
        return {"x-sx-api-key": secrets.get("sxbet_api_key") or ""}

    async def connect(self, key: str | None = None) -> tuple[bool, str]:
        """Log in with the stored key (or `key`, which is then stored). (ok, message)."""
        if not secrets.get("sxbet_api_key"):
            return False, "prvo unesi SX Bet API ključ (🏦 Kladionice → 🔑 SX Bet ključ)"
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
        try:
            address = address_of(stored)
        except ImportError:
            return False, "nije instalirana biblioteka eth-account (pokreni start.bat ponovo)"
        except Exception as e:
            return False, f"ključ nije dobar: {e}"
        try:
            r = await self.client.get(f"{API}/metadata/obv3")
            r.raise_for_status()
            meta = r.json().get("data") or {}
        except Exception as e:
            return False, f"SX Bet ne odgovara: {_err(e)}"
        if not meta.get("domain") or not (meta.get("activeAsset") or {}).get("baseToken"):
            return False, "SX Bet nije poslao podatke za potpis (metadata/obv3)"
        if key:
            try:
                vault.put(KEY_NAME, key)
            except vault.VaultError as e:
                return False, str(e)
        self._key, self._meta, self.address, self.error = stored, meta, address, ""
        self._balance = None
        bal = await self.balance(force=True)
        if bal is None:
            return True, f"povezano ({address[:6]}…{address[-4:]}), ali balans nije pročitan: {self.error}"
        return True, f"povezano ({address[:6]}…{address[-4:]}), balans {bal:.2f} $"

    def forget(self) -> None:
        self._key = None
        self._balance = None
        try:
            vault.delete(KEY_NAME)
        except vault.VaultError:
            pass

    async def balance(self, force: bool = False) -> float | None:
        """USDC available for betting in the V3 trading balance ($), cached BALANCE_TTL s."""
        if not self.connected:
            return None
        if not force and self._balance and time.time() - self._balance[0] < BALANCE_TTL:
            return self._balance[1]
        token = str(self._meta["activeAsset"]["baseToken"]).lower()
        try:
            r = await self.client.get(f"{API}/user/balance-v3", headers=self._headers())
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("balances") or []
            value = sum(int(x.get("availableAmount") or 0) for x in rows
                        if str(x.get("tokenAddress") or "").lower() == token) / 1e6
        except Exception as e:
            self.error = _err(e)
            log.warning("SX Bet balance failed: %s", e)
            return self._balance[1] if self._balance else None
        self._balance = (time.time(), value)
        return value

    def build(self, ref: dict, stake: float, min_odd: float) -> dict | None:
        """The signed order body for backing ref's outcome with `stake` $ at `min_odd` or better."""
        step = int(self._meta.get("oddsLadderStepSize") or LADDER_DEFAULT)
        odds = max_prob(min_odd, step)
        if odds is None:
            return None
        order = {
            "marketHash": ref["market"],
            "baseToken": self._meta["activeAsset"]["baseToken"],
            "totalBetSize": int(math.floor(stake * 100 + 1e-9)) * 10_000,  # USDC has 6 decimals, cents only
            "percentageOdds": odds,
            "salt": pyrandom.randbits(128),
            "expiry": int(time.time()) + ORDER_EXPIRY,
            "maker": self.address,
            "isMakerBettingOutcomeOne": bool(ref["one"]),
        }
        sig = sign_order(order, self._meta["domain"], self._key)
        return {**order, "totalBetSize": str(order["totalBetSize"]), "percentageOdds": str(order["percentageOdds"]),
                "salt": "0x" + format(order["salt"], "064x"), "timeInForce": "FOK", "orderSignature": sig}

    async def buy(self, ref: dict, stake: float, min_odd: float, dry: bool = False) -> Fill:
        """Back ref's outcome with `stake` $ at `min_odd` (after fee) or better - all of it or nothing."""
        if not self.connected:
            return Fill(False, error="SX Bet nije povezan")
        stake = math.floor(stake * 100) / 100
        if stake < MIN_ORDER:
            return Fill(False, error=f"SX Bet ne prima manje od {MIN_ORDER:g} $")
        async with self._lock:
            try:
                body = self.build(ref, stake, min_odd)
            except Exception as e:
                return Fill(False, error=f"potpis nije uspeo: {_err(e)}")
            if body is None:
                return Fill(False, error="nema cene na kojoj se ovo isplati")
            worst = odd_of(int(body["percentageOdds"]))
            if dry:  # signed, but not sent
                return Fill(True, "proba", stake, stake * worst)
            try:
                r = await self.client.post(f"{API}/orders-v3", headers=self._headers(),
                                           json={"orders": [body], "waitForOutcome": True})
                data = r.json()
            except httpx.TimeoutException:
                return Fill(False, stake=stake, unknown=True, error="SX Bet nije odgovorio na vreme")
            except Exception as e:
                return Fill(False, stake=stake, error=_err(e))
        self._balance = None  # changed
        if r.status_code >= 400:
            return Fill(False, stake=stake, error=_api_error(data))
        fill = parse_order(data, stake, worst)
        if fill.unknown and fill.order_id:
            fill = await self._check(fill, stake, worst)
        return fill

    async def _check(self, fill: Fill, stake: float, worst: float) -> Fill:
        """Matching didn't finish in time (TIMEOUT): ask for the order a few times."""
        for _ in range(CHECKS_AFTER_TIMEOUT):
            await asyncio.sleep(2)
            try:
                r = await self.client.get(f"{API}/orders-v3/{fill.order_id}", headers=self._headers())
                data = r.json().get("data") or {}
            except Exception:
                continue
            row = data.get("order", data)
            filled = _amount(row.get("fillAmount"))
            status = str(row.get("status") or row.get("state") or "").upper()
            if filled and filled > 0:
                return Fill(True, fill.order_id, filled, filled * worst)
            if status in ("CANCELLED", "CANCELED", "EXPIRED", "FAILED"):
                return Fill(False, fill.order_id, stake, error=f"nalog nije izvršen ({status.lower()})")
        return fill


def parse_order(data: dict, stake: float, worst: float) -> Fill:
    """POST /orders-v3 answer -> Fill. Filled FOK: {"data": {"orders": [{"orderId": ..., "status": ...,
    "outcome": {"state": "FULLY_FILLED", "fillAmount": "2000000", "remainingAmount": "0"}}]}}.
    The payout is counted at the worst odds we allowed - the real fill is that or better."""
    rows = ((data or {}).get("data") or {}).get("orders") or []
    if not rows:
        return Fill(False, stake=stake, error=_api_error(data))
    row = rows[0]
    oid = str(row.get("orderId") or row.get("orderHash") or "")
    if str(row.get("status") or "").upper() == "FAILED":
        return Fill(False, oid, stake, error=str(row.get("reason") or "odbijeno"))
    out = row.get("outcome") or {}
    state = str(out.get("state") or "").upper()
    filled = _amount(out.get("fillAmount"))
    if state == "FULLY_FILLED" or (state == "PARTIAL_FILL_DONE" and filled):
        got = filled if filled else stake
        return Fill(True, oid, got, got * worst)
    if state == "CANCELLED":
        reason = str(out.get("cancelReason") or out.get("reason") or "").upper()
        why = "nema dovoljno ponude po toj kvoti" if reason == "NO_LIQUIDITY" else reason.lower() or "otkazano"
        return Fill(False, oid, stake, error=f"nalog nije izvršen ({why})")
    if state in ("RESTED", "PARTIAL_FILL_RESTED"):  # must never happen with FOK - treat as unknown
        return Fill(False, oid, stake, unknown=True, error=f"nalog je ostao u knjizi ({state.lower()})")
    return Fill(False, oid, stake, unknown=True, error=f"SX Bet nije potvrdio ishod ({state.lower() or '?'})")


def _amount(v) -> float | None:
    try:
        return int(v) / 1e6
    except (TypeError, ValueError):
        return None


def _api_error(data) -> str:
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or data
        if isinstance(msg, list):
            msg = "; ".join(map(str, msg))
        return str(msg)[:200]
    return str(data)[:200]


def _err(e: Exception) -> str:
    return (str(e) or type(e).__name__)[:200]
