"""Real (automatic) betting: 1xBit + Polymarket.

vault.py      secrets in the OS key store (Windows Credential Manager), never in a file
book.py       the ticket book (data/live.db): every bet, order id, result, balances
polymarket.py Polymarket executor - the official CLOB API (py-clob-client)
onexbit.py    1xBit executor - the user's own logged-in browser, bet request learned once
engine.py     the automatic trader: rules, risk limits, order of the legs, settling"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    """What a bookie answered to one bet."""
    ok: bool
    order_id: str = ""
    stake: float = 0.0  # $ placed
    payout: float = 0.0  # $ it returns if it wins (Polymarket: shares, fee taken off)
    price: float = 0.0  # Polymarket: average price per share
    error: str = ""
    unknown: bool = False  # no answer in time: the ticket may or may not be in - check on the site

    @property
    def odd(self) -> float:
        return self.payout / self.stake if self.stake else 0.0
