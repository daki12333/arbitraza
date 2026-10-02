"""Real (automatic) betting: SX Bet + Polymarket, both through their official APIs.

vault.py      secrets in the OS key store (Windows Credential Manager), never in a file
book.py       the ticket book (data/live.db): every bet, order id, result
sxbet.py      SX Bet executor - V3 order book API (signed EIP-712 order, fill-or-kill)
polymarket.py Polymarket executor - the official CLOB API (py-clob-client)
engine.py     the automatic trader: rules, risk limits, order of the legs, settling
stats.py      how many SX Bet + Polymarket arbs there are (for the 📈 Procena estimate)"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    """What an exchange answered to one bet."""
    ok: bool
    order_id: str = ""
    stake: float = 0.0  # $ placed
    payout: float = 0.0  # $ it returns if it wins (fees taken off)
    price: float = 0.0  # Polymarket: average price per share
    error: str = ""
    unknown: bool = False  # no clear answer in time: the bet may or may not be in - check on the site

    @property
    def odd(self) -> float:
        return self.payout / self.stake if self.stake else 0.0
