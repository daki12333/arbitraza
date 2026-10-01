"""Per-user settings, persisted to data/users.json."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from arb.config import DATA_DIR
from arb.scrapers import ALL_SCRAPERS

ALL_BOOKIES = [cls.name for cls in ALL_SCRAPERS]
# "rs" = Serbian licensed bookies, "crypto" = crypto sportsbooks (Stake, ...)
BOOKIE_REGION = {cls.name: cls.region for cls in ALL_SCRAPERS}
CURRENCY = {"rs": "din", "crypto": "$"}
DEFAULT_BUDGET = {"rs": 50_000, "crypto": 100}
FILE = DATA_DIR / "users.json"
# bookies that existed when settings stored an "enabled" list (pre-migration format)
_LEGACY_BOOKIES = ["Mozzart", "Meridian", "MaxBet", "Admiral", "Soccerbet"]


@dataclass
class UserSettings:
    budget: float = 50_000  # what the user puts into one arb in total, in the current mode's currency
    mode: str = "rs"  # "rs" = Serbian bookies (din), "crypto" = crypto sportsbooks ($)
    other_budget: float = DEFAULT_BUDGET["crypto"]  # the budget of the mode that is switched off
    notify: bool = True
    notify_min: float = 1.0  # only push arbs with at least this profit %; the list shows all
    notify_hours: int = 0  # only push arbs whose match starts within this many hours (0 = any time)
    paper: bool = False  # crypto: play arbs on paper (arb.paper) and report how they would have gone
    list_hours: int = 0  # list only matches starting within this many hours (0 = all)
    list_sort: str = "pct"  # "pct" = best profit first, "time" = soonest first
    # store the switched-OFF ones, so newly added bookies are on by default
    disabled: list[str] = field(default_factory=list)

    @property
    def bookies(self) -> list[str]:
        """Enabled bookies of the current mode."""
        return [b for b in self.mode_bookies if b not in self.disabled]

    @property
    def mode_bookies(self) -> list[str]:
        return [b for b in ALL_BOOKIES if BOOKIE_REGION[b] == self.mode]

    @property
    def currency(self) -> str:
        return CURRENCY[self.mode]

    def switch_mode(self) -> None:
        """Serbian <-> crypto; each mode keeps its own budget (din vs $)."""
        self.mode = "crypto" if self.mode == "rs" else "rs"
        self.budget, self.other_budget = self.other_budget, self.budget


class Storage:
    def __init__(self) -> None:
        self.users: dict[int, UserSettings] = {}
        if FILE.exists():
            raw = json.loads(FILE.read_text(encoding="utf-8"))
            for uid, s in raw.items():
                if "bookies" in s and "disabled" not in s:
                    s["disabled"] = [b for b in _LEGACY_BOOKIES if b not in s["bookies"]]
                known = {k: v for k, v in s.items() if k in UserSettings.__dataclass_fields__}
                self.users[int(uid)] = UserSettings(**known)

    def get(self, uid: int) -> UserSettings:
        if uid not in self.users:
            self.users[uid] = UserSettings()
            self.save()
        return self.users[uid]

    def save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        FILE.write_text(
            json.dumps({str(k): asdict(v) for k, v in self.users.items()}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
