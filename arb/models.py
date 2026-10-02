from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Unified market keys and the outcomes that together cover every result.
# An arbitrage is only valid when a full outcome set is available.
MARKETS: dict[str, tuple[str, ...]] = {
    "1X2": ("1", "X", "2"),
    "OU_1.5": ("U", "O"),
    "OU_2.5": ("U", "O"),
    "OU_3.5": ("U", "O"),
    "BTTS": ("GG", "NG"),
    "12": ("1", "2"),  # tennis / volleyball / table tennis: match winner
    "12_OT": ("1", "2"),  # basketball: winner including overtime
}

# 1X2 is always the regular-time result (football, hockey, handball, basketball without OT).
MARKET_LABELS = {
    "1X2": "Konačan ishod",
    "OU_1.5": "Ukupno golova 1.5",
    "OU_2.5": "Ukupno golova 2.5",
    "OU_3.5": "Ukupno golova 3.5",
    "BTTS": "Oba tima daju gol",
    "12": "Pobednik meča",
    "12_OT": "Pobednik (sa produžecima)",
    "1+X2": "Konačan ishod + dupla šansa",
    "X+12": "Konačan ishod + dupla šansa",
    "2+1X": "Konačan ishod + dupla šansa",
}

# "DC" (double chance: 1X / 12 / X2) is scraped but never arbed on its own;
# together with the opposite single outcome it covers every result:
#   1 + X2,  X + 12,  2 + 1X
COMBOS: dict[str, tuple[tuple[str, str], ...]] = {
    "1+X2": (("1X2", "1"), ("DC", "X2")),
    "X+12": (("1X2", "X"), ("DC", "12")),
    "2+1X": (("1X2", "2"), ("DC", "1X")),
}

SPORTS = ("football", "basketball", "tennis", "hockey", "handball", "volleyball", "table_tennis",
          # crypto books only
          "baseball", "american_football", "cs2", "dota2", "lol", "valorant")
SPORT_ICONS = {"football": "⚽", "basketball": "🏀", "tennis": "🎾", "hockey": "🏒",
               "handball": "🤾", "volleyball": "🏐", "table_tennis": "🏓", "baseball": "⚾",
               "american_football": "🏈", "cs2": "🎮", "dota2": "🎮", "lol": "🎮", "valorant": "🎮"}
SPORT_SLUGS = {"football": "fudbal", "basketball": "kosarka", "tennis": "tenis", "hockey": "hokej",
               "handball": "rukomet", "volleyball": "odbojka", "table_tennis": "stoni-tenis"}

OUTCOME_LABELS = {
    "OU_1.5": {"U": "0-1", "O": "2+"},
    "OU_2.5": {"U": "0-2", "O": "3+"},
    "OU_3.5": {"U": "0-3", "O": "4+"},
}


def split_market(market: str) -> tuple[str, str, str]:
    """(period, team, base): "H1_OU_1.5" -> ("H1", "", "OU_1.5"), "T2_OU_0.5" -> ("", "T2", "OU_0.5").
    H1 = first half, T1/T2 = goals of the home/away team only."""
    period = team = ""
    if market.startswith("H1_"):
        period, market = "H1", market[3:]
    if market.startswith(("T1_", "T2_")):
        team, market = market[:2], market[3:]
    return period, team, market


def outcome_label(market: str, outcome: str) -> str:
    _, _, base = split_market(market)
    if base.startswith("OU_"):  # worded like the sites' "Total" market (not "Goal range")
        return ("Više " if outcome == "O" else "Manje ") + base[3:] + (" (Over)" if outcome == "O" else " (Under)")
    if base.startswith("AH_"):
        line = float(base[3:])
        return f"{outcome} {line_str(line if outcome == '1' else -line, sign=True)}"
    return outcome


# ---- lines: totals "OU_<line>" and two-way handicaps "AH_<home line>" for any line.
# Whole, .5 and Asian quarter lines (2.25, -0.75). Both sides of the SAME quarter line
# still cover every result: on the split result one leg half-wins and the other
# half-loses, which leaves half the arb profit (see quarter_result). Whole lines can
# push (both stakes back, profit 0). Only crypto scrapers call this - the Serbian ones
# use fixed lines.

def line_str(x, sign: bool = False) -> str:
    f = float(x) + 0.0  # no "-0"
    return f"{f:+g}" if sign and f else f"{f:g}"


def clean_line(x) -> bool:
    try:
        return (float(x) * 4).is_integer()
    except (TypeError, ValueError):
        return False


def line_kind(line: float) -> str:
    """"half" (2.5: always decided), "whole" (3: can push) or "quarter" (2.25: can split)."""
    frac = abs(float(line)) % 1
    return "whole" if frac == 0 else "half" if frac == 0.5 else "quarter"


def market_outcomes(market: str) -> tuple[str, ...]:
    period, team, base = split_market(market)
    if team and not base.startswith("OU_"):
        return ()
    if base in MARKETS:
        return MARKETS[base]
    if base.startswith("OU_"):
        return ("U", "O")
    if base.startswith("AH_"):
        return ("1", "2")
    return ()


_TOTAL_NOUN = {"football": "golova", "hockey": "golova", "handball": "golova", "basketball": "poena",
               "tennis": "gemova", "volleyball": "poena", "table_tennis": "poena", "baseball": "runova",
               "american_football": "poena", "cs2": "mapa", "dota2": "mapa", "lol": "mapa", "valorant": "mapa"}


def market_label(market: str, sport: str = "", home: str = "", away: str = "") -> str:
    period, team, base = split_market(market)
    if base in MARKET_LABELS and not (base.startswith("OU_") and (sport not in ("", "football") or period or team)):
        label = MARKET_LABELS[base]
    elif base.startswith("OU_"):
        noun = _TOTAL_NOUN.get(sport, "")
        who = (home if team == "T1" else away if team == "T2" else "") or {"T1": "domaćina", "T2": "gosta"}.get(team, "")
        label = (f"Ukupno {noun} {who} {base[3:]}" if who else f"Ukupno {noun} {base[3:]}").replace("  ", " ")
    elif base.startswith("AH_"):
        label = f"Hendikep {line_str(base[3:], sign=True)} / {line_str(-float(base[3:]), sign=True)}"
    else:
        label = base
    return f"1. poluvreme – {label}" if period == "H1" else label


def utc_from_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass
class Event:
    bookie: str
    event_id: str
    sport: str
    home: str
    away: str
    start: datetime  # timezone-aware UTC
    league: str = ""
    betradar_id: str | None = None
    url: str = ""  # match page on the bookie's site
    group: str = ""  # id of the matched group, set after grouping (same for every bookie's copy)
    markets: dict[str, dict[str, float]] = field(default_factory=dict)
    # max stake the bookie takes per (market, outcome), in its currency ($) - where it publishes one
    limits: dict[tuple[str, str], float] = field(default_factory=dict)
    # what to click on the site when it words the bet differently, per (market, outcome)
    # e.g. Polymarket X2 = 'No' on "Will India win?"
    how: dict[tuple[str, str], str] = field(default_factory=dict)
    # exchanges (Polymarket): order book per (market, outcome) as [(odd after fee, $ on offer at it), ...],
    # best first - a bigger stake also eats into the worse prices
    depth: dict[tuple[str, str], list[tuple[float, float]]] = field(default_factory=dict)
    # what the exchange's own API needs to bet this outcome, per (market, outcome) - for automatic
    # betting (arb.live): Polymarket {"token", "condition", "rate", ...}, SX Bet {"market", "one"}
    bet_ref: dict[tuple[str, str], dict] = field(default_factory=dict)
    # the bookie writes the teams the other way round than the group (set when the matcher turns it):
    # our "1" is "2" on its site
    reversed: bool = False

    def add(self, market: str, outcome: str, odd) -> None:
        try:
            odd = float(odd)
        except (TypeError, ValueError):
            return
        if odd > 1.0:
            self.markets.setdefault(market, {})[outcome] = odd

    def add_total(self, line, over, under) -> None:
        """Total with this line (goals / points / games); skips quarter lines."""
        if clean_line(line):
            key = f"OU_{line_str(line)}"
            self.add(key, "O", over)
            self.add(key, "U", under)

    def add_handicap(self, home_line, home_odd, away_odd) -> None:
        """Two-way handicap: home gets `home_line`, away the opposite."""
        if clean_line(home_line):
            key = f"AH_{line_str(home_line)}"
            self.add(key, "1", home_odd)
            self.add(key, "2", away_odd)

    @property
    def name(self) -> str:
        return f"{self.home} - {self.away}"
