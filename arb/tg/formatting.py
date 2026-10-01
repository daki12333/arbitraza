from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from arb.arbitrage import Arb
from arb.models import (_TOTAL_NOUN, COMBOS, OUTCOME_LABELS, SPORT_ICONS, line_kind, line_str, market_label,
                        outcome_label, split_market)
from arb.scanner import ScanResult

TZ = ZoneInfo("Europe/Belgrade")


def outcome_text(arb: Arb, outcome: str, short: bool = False) -> str:
    """Name the side instead of a bare "1"/"2": bookies order players differently,
    so "1" on one site can be the other player on another site."""
    ev = arb.event
    side = {"1": ev.home, "2": ev.away}.get(outcome)
    market = split_market(arb.market)[2]  # without the first-half / team prefix
    if market in ("12", "12_OT") and side:
        return escape(side)
    if market.startswith("AH_") and side and not short:
        return f"{escape(side)} {outcome_label(arb.market, outcome).split(' ', 1)[1]}"
    base = outcome_label(arb.market, outcome)
    if short:
        return base
    if arb.market in OUTCOME_LABELS and outcome in OUTCOME_LABELS[arb.market]:  # full-time match goals
        return f"{base} = {OUTCOME_LABELS[arb.market][outcome]} gola ukupno"
    if market in ("1X2", *COMBOS) and side:
        return f"{base} · {escape(side)}"
    home, away = escape(ev.home), escape(ev.away)
    explain = {
        "X": "nerešeno",
        "1X": f"{home} ili nerešeno",
        "X2": f"nerešeno ili {away}",
        "12": "bez nerešenog",
    }
    if market in COMBOS and outcome in explain:
        return f"{base} ({explain[outcome]})"
    return base


def fmt_odd(odd: float) -> str:
    """2.10 / 1.465 - three decimals only when they exist (exchange odds after fees)."""
    return f"{odd:.2f}" if round(odd, 2) == round(odd, 3) else f"{odd:.3f}"


def money(x: float, currency: str = "din") -> str:
    """1.234 (din) / 1.234,5 ($, up to 2 decimals: crypto stakes can be 10,5)."""
    if currency == "$" and round(x, 2) != round(x):
        whole, frac = f"{x:,.2f}".split(".")
        return whole.replace(",", ".") + "," + frac.rstrip("0")
    return f"{x:,.0f}".replace(",", ".")


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} s"
    return f"{int(seconds // 60)} min"


def profit_icon(p: float) -> str:
    return "🟢" if p >= 1.5 else "🟡" if p >= 0.5 else "⚪"


PROFIT_TABLE = {"din": [10_000, 50_000, 100_000, 200_000], "$": [50, 100, 250, 500]}


def signed(x: float, currency: str = "din") -> str:
    return ("+" if x >= 0 else "-") + money(abs(x), currency)


def profit_for(arb: Arb, total: float, currency: str = "din") -> float | None:
    """Guaranteed profit for this total, None if the arb doesn't work for it (see Arb.plan)."""
    rows = arb.plan(total, currency)
    if not rows:
        return None
    return min(p for _, _, p in rows) - sum(s for _, s, _ in rows)


_SITE_FLIP = {"1": "2", "2": "1", "1X": "X2", "X2": "1X"}


def site_label(leg) -> str | None:
    """How a bookie that writes the teams the other way round names this pick (None = same)."""
    period, team, base = split_market(leg.market)
    if base.startswith("AH_"):  # the same team keeps its handicap, it is just the other number there
        line = float(base[3:])
        return f"{_SITE_FLIP[leg.outcome]} ({line_str(line if leg.outcome == '1' else -line, sign=True)})"
    if base in ("1X2", "12", "12_OT", "DC") and leg.outcome in _SITE_FLIP:
        return _SITE_FLIP[leg.outcome]
    return None


def line_of(arb: Arb) -> tuple[str, float] | None:
    """("OU" / "AH", line) for totals and handicaps, else None."""
    base = split_market(arb.market)[2]
    return (base[:2], float(base[3:])) if base.startswith(("OU_", "AH_")) else None


def split_result(arb: Arb, rows) -> tuple[str, float, str] | None:
    """Whole and Asian quarter lines have one result that is neither a win nor a loss:
    returns (that result in words, the return of both bets on it, what happens), else None."""
    ol = line_of(arb)
    if not ol or line_kind(ol[1]) == "half":
        return None
    kind, line = ol
    period, team, _ = split_market(arb.market)
    ev = arb.event
    if kind == "OU":
        n = round(line)  # 2.25 -> 2, 2.75 -> 3, 3 -> 3
        positive_wins = n > line  # does Over half-win there?
        noun = _TOTAL_NOUN.get(ev.sport, "")
        if noun == "golova":  # 1 gol, 2-4 gola, 5+ golova
            noun = "gol" if n % 10 == 1 and n % 100 != 11 else "gola" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else noun
        who = {"T1": f"{ev.home} da ", "T2": f"{ev.away} da "}.get(team, "")
        label = f"{who}tačno {n} {noun}".strip() if who else f"ukupno tačno {n} {noun}".strip()
    else:
        m = round(-line)  # home winning margin where home + line lands on 0 / ±0.25
        positive_wins = m + line > 0  # does the home side half-win there?
        label = ("nerešeno" if m == 0 else
                 f"{ev.home} pobedi razlikom od tačno {m}" if m > 0 else f"{ev.away} pobedi razlikom od tačno {-m}")
    if period:
        label += " (1. poluvreme)"
    back = 0.0
    for leg, s, p in rows:
        if line_kind(line) == "whole":
            back += s  # push: stake back
        elif (leg.outcome in ("O", "1")) == positive_wins:
            back += s / 2 + p / 2  # half won, half back
        else:
            back += s / 2  # half lost, half back
    what = "oba uloga se vraćaju" if line_kind(line) == "whole" else "pola tiketa se vraća"
    return label, back, what


def arb_text(arb: Arb, budget: float, age: float, currency: str = "din") -> str:
    ev = arb.event
    start = ev.start.astimezone(TZ).strftime("%d.%m. %H:%M")
    head = [
        f"{SPORT_ICONS.get(ev.sport, '🏅')} <b>{escape(ev.home)} – {escape(ev.away)}</b>",
        f"🏆 {escape(ev.league)}  ·  🕒 {start}",
    ]
    # round amounts, so the bets don't look calculated; None = can't be placed / no profit for this stake
    rows = arb.plan(budget, currency)
    if not rows:
        return "\n".join(head + [
            f"📊 {escape(market_label(arb.market, ev.sport, ev.home, ev.away))}",
            "",
            f"❌ Za ulog od <b>{money(budget, currency)}</b> {currency} ova arbitraža ne prolazi – "
            "kladionica po toj kvoti ne prima toliko ili zarada nestaje. Probaj manji ulog.",
        ])
    target = budget
    total = sum(s for _, s, _ in rows)
    worst = min(p for _, _, p in rows)
    best = max(p for _, _, p in rows)

    lines = head + [
        f"📊 {escape(market_label(arb.market, ev.sport, ev.home, ev.away))}  ·  <b>{(worst - total) / total * 100:+.2f}%</b> {profit_icon((worst - total) / total * 100)}",
    ]
    if arb.has_depth:
        lines.append("📉 Polymarket kvota je prosečna za tvoj ulog (sa većim ulogom pada)")
    lines += ["", f"💰 Za <b>{money(target, currency)}</b> {currency} uloži:"]
    for leg, s, p in rows:
        bookie = bookie_link(arb, leg.bookie)
        odd = f"kvota <b>{fmt_odd(leg.odd)}</b>"
        if leg.depth and s > 0 and p / s < leg.odd - 0.0005:
            odd = f"kvota {fmt_odd(leg.odd)}, za ovaj ulog prosečno <b>{fmt_odd(p / s)}</b>"
        if arb.fresh is not None:  # just re-checked: was this bookie's odd really fetched now?
            before = arb.prev_odds.get((leg.market, leg.outcome))
            moved = f" (bila {fmt_odd(before)})" if before and abs(before - leg.odd) > 0.0005 else ""
            odd += f" ✅{moved}" if leg.bookie in arb.fresh else " ⏳ (nije stigla nova, stara kvota)"
        lines.append(
            f"<b>{outcome_text(arb, leg.outcome)}</b> → {bookie} {odd}"
            f"  ·  uloži <b>{money(s, currency)}</b>"
        )
        ev = arb.event_for(leg.bookie)
        how = ev.how.get((leg.market, leg.outcome)) if ev else None
        if how:
            lines.append(f"   👉 na {leg.bookie}: {escape(how)}")
        elif ev is not None and ev.reversed and (site := site_label(leg)):
            lines.append(f"   👉 na {leg.bookie} su timovi napisani obrnuto ({escape(ev.away)} – {escape(ev.home)}): "
                         f"tamo klikni <b>{site}</b>")
    diff = total - target
    note = f"  ({money(target, currency)}, {signed(diff, currency)} zbog zaokruživanja)" if abs(diff) >= (0.01 if currency == "$" else 1) else ""
    lines += ["", f"💵 Ukupno uplaćuješ <b>{money(total, currency)}</b> {currency}{note}", "", "🎲 Ako prođe:"]
    for leg, s, p in rows:
        lines.append(f"  {outcome_text(arb, leg.outcome)} → isplata {money(p, currency)}  ·  profit <b>{signed(p - total, currency)}</b>")
    if sr := split_result(arb, rows):  # Asian quarter / whole line: one result settles in halves / pushes
        label, back, what = sr
        lines.append(f"  {escape(label)} → {what}, isplata {money(back, currency)}  ·  profit <b>{signed(back - total, currency)}</b>")
        worst = min(worst, back)
    lines += [
        "",
        f"✅ Zarada min <b>{signed(worst - total, currency)}</b>  ·  max <b>{signed(best - total, currency)}</b> {currency}",
        "",
        "📈 Min. profit za ulog:",
    ]
    cells = [f"{money(t, currency)} → " + (f"<b>{signed(g, currency)}</b>" if (g := profit_for(arb, t, currency)) is not None
                                            else "ne prolazi") for t in PROFIT_TABLE[currency]]
    lines += ["  ·  ".join(cells[i:i + 2]) for i in range(0, len(cells), 2)]
    lines += [
        "",
        f"⏱ kvote od pre {ago(age)}" + ("  ·  ✅ = upravo proverena na sajtu" if arb.fresh is not None else ""),
    ]
    names = {e.name for e in arb.events if any(l.bookie == e.bookie for l in arb.legs)}
    if len(names) > 1:
        lines.append("ℹ️ nazivi: " + escape(" / ".join(sorted(names))))
    if arb.suspicious:
        lines.append("\n⚠️ <b>Sumnjivo visok profit</b> – proveri meč i market pre uplate.")
    return "\n".join(lines)


PAGE_SIZE = 10  # ~200 visible chars per arb keeps a page far below Telegram's 4096


def bookie_link(arb: Arb, bookie: str) -> str:
    e = arb.event_for(bookie)
    return f'<a href="{escape(e.url)}">{bookie}</a>' if e and e.url else bookie


def page_count(n: int) -> int:
    return max(1, -(-n // PAGE_SIZE))


def list_text(arbs: list[Arb], budget: float, age: float, live: bool, page: int,
              hours: int = 0, sort: str = "pct", hidden: int = 0, currency: str = "din") -> str:
    """One page of the (already filtered and sorted) arb list; page is 0-based and clamped."""
    now = datetime.now(TZ).strftime("%H:%M:%S")
    pages = page_count(len(arbs))
    view = ("sve utakmice" if not hours else f"počinju u narednih {hours}h") + "  ·  " + (
        "najranije prvo" if sort == "time" else "najveći profit prvo")
    head = [
        f"📋 <b>Lista arbitraža</b> ({len(arbs)})  ·  ulog <b>{money(budget, currency)}</b> {currency}",
        f"🕒 {view}",
        *([f"⚠️ <b>{hidden} arbitraža sakriveno filterom</b> (kasniji mečevi) – klikni <b>Sve</b> ispod"]
          if hidden else []),
        (f"🔄 prati se uživo · ažurirano {now} (kvote od pre {ago(age)})" if live
         else f"⏸ praćenje zaustavljeno · {now}"),
        "",
    ]
    if not arbs:
        return "\n".join(head + ["😴 Trenutno nema arbitraža. Lista će se sama popuniti kad se pojave."])

    body = []
    marks = set()
    first = page * PAGE_SIZE
    for i, arb in enumerate(arbs[first:first + PAGE_SIZE], first + 1):
        ev = arb.event
        start = ev.start.astimezone(TZ).strftime("%d.%m. %H:%M")
        legs = "  ·  ".join(
            f"<b>{outcome_text(arb, l.outcome, short=True)}</b> {fmt_odd(l.odd)} {bookie_link(arb, l.bookie)}" for l in arb.legs
        )
        # same stakes as the arb's detail message (the list only holds arbs that work for this budget)
        gain = profit_for(arb, budget, currency) or 0.0
        pct = arb.pct_for(budget, currency)
        ol = line_of(arb)
        mark = {"quarter": " ½", "whole": " ↩"}.get(line_kind(ol[1]) if ol else "", "")
        marks.add(mark)
        body.append(
            f"<b>{i}.</b> {profit_icon(pct)} <b>{pct:+.2f}%</b>{mark}  ·  zarada "
            f"<b>{signed(gain, currency)}</b>{'  ⚠️' if arb.suspicious else ''}\n"
            f"{SPORT_ICONS.get(ev.sport, '🏅')} {escape(ev.home)} – {escape(ev.away)}  ·  {start}\n"
            f"{escape(market_label(arb.market, ev.sport, ev.home, ev.away))}: {legs}\n"
        )
    legend = [x for m, x in ((" ½", "½ = azijska linija: na jednom tačnom rezultatu pola profita"),
                             (" ↩", "↩ = cela linija: na jednom tačnom rezultatu ulog se vraća")) if m in marks]
    foot = [*legend, f"📄 Strana {page + 1}/{pages}" if pages > 1 else "", "👇 Klikni broj za uloge i linkove."]
    return "\n".join(head + body + foot)


def status_text(res: ScanResult | None, age: float, interval: int) -> str:
    if not res:
        return "⏳ Prvi scan je u toku, probaj za pola minuta."
    lines = [f"📊 <b>Status</b>  (poslednji scan pre {ago(age)}, na svakih {interval} s)", ""]
    for name, r in res.bookies.items():
        if r.error:
            lines.append(f"❌ {name}: greška ({escape(r.error[:80])})")
        else:
            lines.append(f"✅ {name}: {len(r.events)} mečeva  ·  {r.seconds:.1f} s")
    multi = sum(1 for g in res.groups if len(g) > 1)
    lines += ["", f"🔗 Upareno mečeva (2+ kladionice): <b>{multi}</b>", f"💰 Arbitraža ukupno: <b>{len(res.arbs)}</b>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- middles

MIDDLES_PAGE = 6


def _middle_leg(m, leg) -> str:
    ev = m.event
    line = m.low if leg is m.legs[0] else m.high
    if m.kind == "OU":
        return f"{'Više' if leg.outcome == 'O' else 'Manje'} {line:g}"
    side, l = (ev.home, line) if leg.outcome == "1" else (ev.away, -line)
    return f"{escape(side)} {l:+g}"


def _middle_window(m) -> str:
    ev = m.event
    w = m.window
    span = f"{w[0]}" if len(w) == 1 else f"{w[0]}–{w[-1]}"
    if m.kind == "AH":
        if w[-1] < 0:  # negative home margin = the away team wins by that much
            span = f"{-w[0]}" if len(w) == 1 else f"{-w[-1]}–{-w[0]}"
            return f"{escape(ev.away)} pobedi razlikom {span}"
        if w[0] <= 0 <= w[-1]:
            return f"razlika bude od {w[0]} do {w[-1]} za {escape(ev.home)}"
        return f"{escape(ev.home)} pobedi razlikom {span}"
    noun = {"football": "gol", "hockey": "gol", "handball": "gol", "basketball": "poen", "tennis": "gem",
            "baseball": "run", "american_football": "poen"}.get(ev.sport, "")
    who = {"T1_": f" ({escape(ev.home)})", "T2_": f" ({escape(ev.away)})"}.get(m.prefix.replace("H1_", ""), "")
    half = " u 1. poluvremenu" if m.prefix.startswith("H1_") else ""
    return f"padne {'tačno ' if len(w) == 1 else ''}{span} {noun}{'a' if noun and w[-1] != 1 else ''}{who}{half}"


def middles_text(middles: list, budget: float, currency: str, page: int, age: float) -> str:
    from arb.arbitrage import _round_step

    pages = max(1, -(-len(middles) // MIDDLES_PAGE))
    page = min(page, pages - 1)
    lines = [
        f"🎯 <b>Srednjice</b> ({len(middles)})  ·  ulog <b>{money(budget, currency)}</b> {currency}  ·  kvote od pre {ago(age)}",
        "Dve različite linije: ako rezultat „upadne u sredinu“ prolaze <b>oba</b> tiketa, "
        "inače prolazi jedan i gubiš malo. Nije sigurna zarada kao arbitraža.",
        "",
    ]
    if not middles:
        return "\n".join(lines + ["😴 Trenutno nema dobrih srednjica."])
    first = page * MIDDLES_PAGE
    for i, m in enumerate(middles[first:first + MIDDLES_PAGE], first + 1):
        ev = m.event
        start = ev.start.astimezone(TZ).strftime("%d.%m. %H:%M")
        stakes = []
        for leg in m.legs:
            raw = budget * (1 / leg.odd) / m.inv
            step = _round_step(raw, currency)
            stakes.append(max(step, round(raw / step) * step))
        total = sum(stakes)
        one = min(s * l.odd for s, l in zip(stakes, m.legs)) - total
        both = sum(s * l.odd for s, l in zip(stakes, m.legs)) - total
        legs = "  +  ".join(
            f"<b>{_middle_leg(m, l)}</b> {fmt_odd(l.odd)} {bookie_link(m, l.bookie)} uloži <b>{money(s, currency)}</b>"
            for l, s in zip(m.legs, stakes))
        hows = [f"   👉 na {l.bookie}: {escape(h)}" for l in m.legs
                if (e := m.event_for(l.bookie)) and (h := e.how.get((l.market, l.outcome)))]
        chance = ("💰 i bez sredine si u plusu" if m.breakeven == 0
                  else f"isplati se ako je šansa za sredinu > {m.breakeven * 100:.0f}%")
        if m.chance is not None:
            chance += f"  ·  procena šanse <b>~{m.chance * 100:.0f}%</b> → očekivano <b>{m.ev * 100:+.1f}%</b>"
        lines += [
            f"<b>{i}.</b> {SPORT_ICONS.get(ev.sport, '🏅')} <b>{escape(ev.home)} – {escape(ev.away)}</b>  ·  {start}",
            f"{escape(market_label(m.legs[0].market, ev.sport, ev.home, ev.away).rsplit(' ', 1)[0] if m.kind == 'OU' else ('1. poluvreme – ' if m.prefix.startswith('H1_') else '') + 'Hendikep')}: {legs}",
            *hows,
            f"🎯 Oba prolaze ako {_middle_window(m)}: <b>{signed(both, currency)}</b> {currency}",
            f"↔️ Inače: <b>{signed(one, currency)}</b> {currency}  ·  {chance}",
            "",
        ]
    if pages > 1:
        lines.append(f"📄 Strana {page + 1}/{pages}")
    return "\n".join(lines)


# ---- paper trading ("test na papiru", arb.paper) ----------------------------

_PAPER_HEAD = {
    "ok": "✅ <b>Prošlo bi</b>",
    "ok_less": "🟡 <b>Prošlo bi, uz manju zaradu</b> (kvota se pomerila)",
    "miss": "❌ <b>Propalo bi</b> – druga noga se pomerila pre uplate",
    "unknown": "❔ <b>Nepoznato</b> – kladionica nije odgovorila na vreme",
}


def paper_text(r, currency: str = "$") -> str:
    """One paper bet: what would have been bet, and whether the last leg still held."""
    lines = [f"🧪 <b>Test na papiru</b> · {escape(r.name)}",
             f"📊 {escape(r.market)}  ·  ukupno {money(r.total, currency)} {currency}"
             + (f" (max {money(r.cap, currency)} – više ne prima)" if r.cap and r.total < r.cap * (1 - 0.03) else ""),
             ""]
    for l in r.legs:
        line = f"{l.order}. {l.bookie}: <b>{l.label}</b> @ {fmt_odd(l.odd)} × {money(l.stake, currency)} {currency}"
        if l.status == "placed":
            line += " → bila bi uplaćena ✅"
        elif l.status == "unknown":
            line += " → nije stigla provera ❔"
        elif l.now_odd is None:
            line += f" → posle {r.seconds:.1f} s <b>više nije u ponudi</b> ❌".replace(".", ",")
        else:
            mark = {"ok": "✅", "worse": "🟡", "miss": "❌"}[l.status]
            moved = "" if abs(l.now_odd - l.odd) < 0.0005 else f" (bila {fmt_odd(l.odd)})"
            line += f" → posle {r.seconds:.1f} s".replace(".", ",") + f" kvota {fmt_odd(l.now_odd)}{moved} {mark}"
        lines.append(line)
    lines += ["", _PAPER_HEAD.get(r.status, r.status)]
    if r.free is not None:
        lines.append(f"💼 slobodno u budžetu posle ove: {money(max(r.free, 0), currency)} {currency}")
    if r.status == "miss":
        if r.hedge:
            lines.append(f"🛟 pokrilo bi se na {escape(r.hedge)} → rezultat <b>{signed(r.profit, currency)}</b> {currency}")
        else:
            lines.append(f"⚠️ nema gde da se pokrije – ostala bi otvorena opklada, najgori ishod "
                         f"<b>{signed(r.profit, currency)}</b> {currency}")
    elif r.status == "unknown":
        lines.append(f"💰 da je prošlo: <b>{signed(r.planned_profit, currency)}</b> {currency}")
    else:
        pct = r.profit / r.total * 100 if r.total else 0
        lines.append(f"💰 zarada <b>{signed(r.profit, currency)}</b> {currency} ({pct:+.2f}%)")
    return "\n".join(lines)


def paper_report(rows: list[dict], since_label: str, currency: str = "$") -> str:
    """Summary of the stored paper tests (arb.paper.load)."""
    if not rows:
        return (f"📊 <b>Test na papiru – {since_label}</b>\n\nJoš nema testova. Bot testira arbitraže koje prolaze "
                "tvoja pravila iz /bot (rok početka meča, najmanji % i najveći ulog).")
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    n = lambda st: len(by.get(st, []))
    won = sum(r["profit"] for st in ("ok", "ok_less") for r in by.get(st, []))
    lost = sum(r["profit"] for r in by.get("miss", []))
    tried = sum(n(st) for st in ("ok", "ok_less", "miss", "unknown"))
    caught = n("ok") + n("ok_less")
    misses: dict[str, int] = {}
    for r in by.get("miss", []):
        last = r["detail"]["legs"][-1]["bookie"] if r["detail"]["legs"] else "?"
        misses[last] = misses.get(last, 0) + 1
    secs = [r["seconds"] for r in rows if r["status"] in ("ok", "ok_less", "miss")]
    lines = [
        f"📊 <b>Test na papiru – {since_label}</b>", "",
        f"🎯 Pokušano: <b>{tried}</b>  ·  uhvaćeno <b>{caught}</b>"
        + (f" ({caught / tried * 100:.0f}%)" if tried else ""),
        f"✅ prošlo: {n('ok')}  ·  🟡 uz manju zaradu: {n('ok_less')}  ·  ❌ propalo: {n('miss')}  ·  ❔ nepoznato: {n('unknown')}",
        f"💨 nestalo pre uplate: {n('gone')}  ·  ne prolazi za ulog: {n('no_fit')}", "",
        f"💰 zarada na uhvaćenim: <b>{signed(won, currency)}</b> {currency}",
        f"❌ rezultat propalih (posle pokrivanja): <b>{signed(lost, currency)}</b> {currency}",
        f"🧾 <b>UKUPNO: {signed(won + lost, currency)} {currency}</b>",
    ]
    if secs:
        lines.append(f"⏱ prosečno od prve do poslednje noge: {sum(secs) / len(secs):.1f} s".replace(".", ","))
    if misses:
        lines.append("📉 gde se kvota pomerila: " + ", ".join(f"{b} {k}" for b, k in sorted(misses.items(), key=lambda x: -x[1])))
    lines += ["", "ℹ️ Test ne vidi da li bi kladionica odbila tiket ili smanjila ulog – to pokazuje tek pravo igranje."]
    return "\n".join(lines)
