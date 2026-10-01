"""Groups the same match across bookmakers.

1. Betradar ID - every bookie here exposes it for most matches, exact and safe.
2. Fallback: kickoff within MAX_TIME_DIFF and both team names fuzzy-similar,
   or identical (normalized) names within PLACEHOLDER_TIME_DIFF.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache

from rapidfuzz import fuzz

from arb.models import Event, line_str, split_market

MAX_TIME_DIFF = timedelta(minutes=10)
MIN_NAME_SCORE = 82
PLACEHOLDER_TIME_DIFF = timedelta(hours=8)

_STOPWORDS = {"fc", "fk", "sc", "cf", "ac", "afc", "cd", "sd", "ud", "ca", "club", "sk", "nk", "bk", "if", "the"}
# gender markers are compared separately (_team_tags), keep them out of the name comparison
_STOPWORDS |= {"women", "wom", "w", "z", "zene", "ladies", "female"}
_TRANSLIT = str.maketrans({"đ": "dj", "Đ": "dj", "ß": "ss", "ø": "o", "æ": "ae", "ł": "l"})


@lru_cache(maxsize=300_000)
def normalize(name: str) -> str:
    name = name.translate(_TRANSLIT)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = name.replace(".", "")  # "N.E.C." == "NEC", "St. Pauli" == "St Pauli"
    name = re.sub(r"\bu[- ]?(\d{2})\b", r"m\1", name)  # U21 == M21
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return " ".join(t for t in name.split() if t not in _STOPWORDS)


@lru_cache(maxsize=500_000)
def _name_score(a: str, b: str) -> float:
    """Similarity of two normalized team names, 0..100. token_set handles extra words
    ("Kosner Baskonia" / "Baskonia"); partial handles cut-offs ("Bosnia Herz" /
    "Bosnia Herzegovina") when the shorter name is long enough to be specific."""
    score = fuzz.token_set_ratio(a, b)
    if score < MIN_NAME_SCORE and min(len(a), len(b)) >= 6:
        partial = fuzz.partial_ratio(a, b)
        if partial >= 92:
            score = max(score, partial - 8)  # a bit below a real full match
    return score


def _pair_score(ref_home: str, ref_away: str, home: str, away: str) -> float:
    """Both teams similar (in this order) -> the weaker of the two scores, else 0."""
    h = _name_score(normalize(ref_home), normalize(home))
    if h < MIN_NAME_SCORE:
        return 0
    a = _name_score(normalize(ref_away), normalize(away))
    return min(h, a) if a >= MIN_NAME_SCORE else 0


def _names_close(ref: Event, home: str, away: str) -> bool:
    return _pair_score(ref.home, ref.away, home, away) > 0


_WOMEN = re.compile(r"\((w|ž|f|women|wom\.?|žene|zene)\)|\b(women|wom\.?|ladies|female|žene|zene)\b|\s(w|ž)\.?$",
                    re.IGNORECASE)
_YOUTH = re.compile(r"\b[um][- ]?(\d{2})\b", re.IGNORECASE)
_RESERVE = re.compile(r"\s(ii|b|2|res\.?|reserves?)$", re.IGNORECASE)


@lru_cache(maxsize=300_000)
def _team_tags(name: str) -> tuple:
    """What a fuzzy name match must never ignore: women's / youth / reserve sides
    ("Lanus (Women)" vs "Lanus", "Slovakia U19" vs "Slovakia", "Bayern II" vs "Bayern")."""
    name = name.strip()
    youth = _YOUTH.search(name)
    return bool(_WOMEN.search(name)), youth.group(1) if youth else "", bool(_RESERVE.search(name))


def _tags(ev: Event) -> tuple:
    """(doubles, women, youth ages, reserve flags) of a match. Youth/women count for the
    whole match: books often mark only one team ("Azerbaijan U19 - UAE")."""
    a, b = _team_tags(ev.home), _team_tags(ev.away)
    return ("/" in ev.home or "/" in ev.away,  # "Cross / Rogers" = doubles, never singles
            a[0] or b[0], frozenset(x for x in (a[1], b[1]) if x), tuple(sorted((a[2], b[2]))))


def _tags_compatible(ref: Event, ev: Event, score: float) -> bool:
    t1, t2 = _tags(ref), _tags(ev)
    if t1 == t2:
        return True
    # one book marks women's games "(w)", another doesn't mark them at all: accept only
    # when everything else is identical, names are near-exact and kickoff is the same
    # (and the odds-consistency check in find_arbs still guards the arb)
    if (t1[0], t1[2], t1[3]) == (t2[0], t2[2], t2[3]) and t1[1] != t2[1]:
        return score >= 95 and abs(ref.start - ev.start) <= timedelta(minutes=5)
    return False


def _match_score(ref: Event, ev: Event) -> float:
    """How well ev matches ref (0 = not the same match): kickoff within MAX_TIME_DIFF,
    compatible tags, and both names similar (either home/away order)."""
    if abs(ref.start - ev.start) > MAX_TIME_DIFF:
        return 0
    score = max(_pair_score(ref.home, ref.away, ev.home, ev.away),
                _pair_score(ref.home, ref.away, ev.away, ev.home))
    return score if score and _tags_compatible(ref, ev, score) else 0


def _same_match(ref: Event, ev: Event) -> bool:
    return _match_score(ref, ev) > 0


def _day_keys(ev: Event, flipped: bool = False) -> list[tuple]:
    """Exact-name keys for the placeholder-kickoff rule (same names, up to 8 h apart)."""
    home, away = (ev.away, ev.home) if flipped else (ev.home, ev.away)
    day = ev.start.date().toordinal()
    return [(ev.sport, d, normalize(home), normalize(away)) for d in (day - 1, day, day + 1)]


def group_events(events: list[Event]) -> list[list[Event]]:
    """Betradar id first; everything else is compared only against groups of the
    same sport starting in the neighbouring 10-minute buckets (an index, not an
    all-pairs scan - that took minutes with ~10k events)."""
    groups: list[list[Event]] = []
    bookies: list[set[str]] = []
    by_br: dict[str, int] = {}
    near: dict[tuple, list[int]] = defaultdict(list)  # (sport, 10-min bucket) -> group idx
    exact: dict[tuple, list[int]] = defaultdict(list)  # (sport, day, home, away) -> group idx

    def new_group(ev: Event) -> int:
        groups.append([ev])
        bookies.append({ev.bookie})
        i = len(groups) - 1
        near[(ev.sport, _bucket(ev))].append(i)
        exact[_day_keys(ev)[1]].append(i)
        return i

    rest: list[Event] = []
    for ev in events:
        if not ev.betradar_id:
            rest.append(ev)
            continue
        key = f"{ev.sport}:{ev.betradar_id}"
        i = by_br.get(key)
        if i is None:
            by_br[key] = new_group(ev)
        elif ev.bookie not in bookies[i]:
            groups[i].append(ev)
            bookies[i].add(ev.bookie)

    for ev in rest:
        b = _bucket(ev)
        found, best = None, 0.0
        for i in (i for k in (b - 1, b, b + 1) for i in near.get((ev.sport, k), ())):
            if ev.bookie in bookies[i]:
                continue
            # compare with every distinct naming in the group ("Lyon" may only match the
            # 1xBit copy, not "Olympique Lyonnais"), keep the best-scoring group
            seen = set()
            for member in groups[i]:
                names = (member.home, member.away)
                if names in seen:
                    continue
                seen.add(names)
                score = _match_score(member, ev)
                if score > best:
                    found, best = i, score
        if found is None:
            # Some books show a placeholder kickoff (e.g. 15:00) until the league
            # fixes the time: identical names within PLACEHOLDER_TIME_DIFF still match.
            for key in _day_keys(ev) + _day_keys(ev, flipped=True):
                for i in exact.get(key, ()):
                    if (ev.bookie not in bookies[i] and abs(groups[i][0].start - ev.start) <= PLACEHOLDER_TIME_DIFF
                            and _tags(groups[i][0]) == _tags(ev)):
                        found = i
                        break
                if found is not None:
                    break
        if found is None:
            new_group(ev)
        else:
            groups[found].append(ev)
            bookies[found].add(ev.bookie)

    for grp in groups:
        orient(grp[0], grp[1:])
        key = next((e.betradar_id for e in grp if e.betradar_id), None) or f"{grp[0].bookie[:2]}{grp[0].event_id}"
        for ev in grp:
            ev.group = key
    return groups


def orient(ref: Event, events: list[Event]) -> None:
    """Turn events to ref's home/away order (in place)."""
    for ev in events:
        if _is_reversed(ref, ev):
            _swap_sides(ev)


def _bucket(ev: Event) -> int:
    return int(ev.start.timestamp() // MAX_TIME_DIFF.total_seconds())


# Bookies don't agree on who is "home": tennis especially ("Storm Hunter - Lin"
# vs "Lin - Hunter Storm" under the same Betradar id). Every event in a group is
# turned to the orientation of the group's first event, otherwise "1" at one
# bookie would be paired with "1" of the other player elsewhere.
SIDED_MARKETS = ("1X2", "12", "12_OT")


def _side_score(a_home: str, a_away: str, b_home: str, b_away: str) -> float:
    return (fuzz.token_set_ratio(normalize(a_home), normalize(b_home))
            + fuzz.token_set_ratio(normalize(a_away), normalize(b_away)))


def _is_reversed(ref: Event, ev: Event) -> bool:
    straight = _side_score(ref.home, ref.away, ev.home, ev.away)
    crossed = _side_score(ref.home, ref.away, ev.away, ev.home)
    # trust names only when one orientation really matches ("Nyon - Basel" vs
    # "Regio - Sdent BBC" shares no words: both scores are noise, use the odds)
    if abs(crossed - straight) >= 20 and max(crossed, straight) >= 140:
        return crossed > straight
    # names don't tell (e.g. "PSG" vs "Paris Saint-Germain"): let the odds decide -
    # the favourite is the favourite at every bookie
    for mk in SIDED_MARKETS:
        a, b = ref.markets.get(mk, {}), ev.markets.get(mk, {})
        if all(k in a and k in b for k in ("1", "2")):
            return (a["1"] < a["2"]) != (b["1"] < b["2"]) and abs(a["1"] - a["2"]) > 0.5 and abs(b["1"] - b["2"]) > 0.5
    return False


_FLIP_OUT = {"1": "2", "2": "1", "1X": "X2", "X2": "1X"}


def _flip_key(mk: str, oc: str) -> tuple[str, str]:
    """(market, outcome) as seen from the other side: home <-> away."""
    period, team, base = split_market(mk)
    prefix = (period + "_" if period else "") + ({"T1": "T2_", "T2": "T1_"}.get(team, ""))
    if base.startswith("AH_"):
        return prefix + "AH_" + line_str(-float(base[3:])), _FLIP_OUT.get(oc, oc)
    if base in SIDED_MARKETS or base == "DC":
        return prefix + base, _FLIP_OUT.get(oc, oc)
    return prefix + base, oc  # totals, BTTS (team totals only change T1 <-> T2)


def _swap_sides(ev: Event) -> None:
    ev.home, ev.away = ev.away, ev.home
    ev.reversed = not ev.reversed  # "1" on the site is now our "2"
    markets: dict[str, dict[str, float]] = {}
    for mk, outs in ev.markets.items():
        for oc, odd in outs.items():
            nk, no = _flip_key(mk, oc)
            markets.setdefault(nk, {})[no] = odd
    ev.markets = markets
    ev.limits = _flip_keys(ev.limits)  # stake limits and site wording follow their outcome
    ev.how = _flip_keys(ev.how)
    ev.depth = _flip_keys(ev.depth)


def _flip_keys(d: dict) -> dict:
    return {_flip_key(mk, oc): v for (mk, oc), v in d.items()}


