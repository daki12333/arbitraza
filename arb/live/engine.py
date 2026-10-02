"""The automatic trader: plays 1xBit + Polymarket arbs for real.

After every scan it looks for arbs between exactly these two bookies that pass the 🤖 rules
(min profit, kickoff window, stake limits) and plays them one at a time:

1. checks: 🛑 not pressed, Polymarket allows this country (geoblock), daily limit, open
   exposure, money on both sides (Polymarket from its API, 1xBit typed in + followed)
2. odds re-read from both bookies right now; the stake fits the limits and both balances
3. 1xBit first: a fixed-odds book can refuse the ticket or the odd - then nothing is lost
4. Polymarket second, FOK ("all or nothing") at the worst price where the whole arb is at
   zero (fee included) - tried RETRIES times; if the book moved away, it covers with a
   loss of at most COVER_LOSS of the total; if even that fails the user is alerted at once

Every ticket goes to the book (arb.live.book). After the match the result is read from
Polymarket (the market resolves) and the profit lands on the balance of the side that won."""
from __future__ import annotations

import asyncio
import logging
import secrets as pyrandom
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from arb.arbitrage import Arb
from arb.live import Fill, book as bk
from arb.live.onexbit import OneXBitExecutor
from arb.live.polymarket import MIN_ORDER, PolyExecutor, resolution, worst_price
from arb.models import market_label
from arb.paper import fit_stake

log = logging.getLogger(__name__)

PAIR = ("1xBit", "Polymarket")
AUTO_MIN_STAKE = 2.0  # $ total - Polymarket takes 1 $ at least per order
MIN_LEAD = 120  # s - the match must start at least this long after the bet
RETRIES = 3  # Polymarket FOK at break-even, this many times
RETRY_WAIT = 2.0  # s between them (the book refills)
COVER_LOSS = 0.05  # then: cover even at a loss of up to 5 % of the total
RETRY_SAME = 10 * 60  # s - an arb tried (or refused) isn't tried again with the same odds before this
CONFIRM_SECONDS = 90  # ✋ "pitaj pre uplate": the button works this long
SETTLE_AFTER = 2 * 3600  # s after kickoff: start asking Polymarket for the result
ASK_AFTER = 36 * 3600  # s after kickoff: still unresolved - the user settles it by hand
PER_ROUND = 3  # arbs played per scan at most
REPORT_HOUR = 23  # Belgrade time
RECONNECT_WAIT = 60  # s between Polymarket login attempts after one failed

UNCONFIRMED = "1xBit nije potvrdio tiket"  # note of a run whose 1xBit ticket may or may not be in

Send = Callable[..., Awaitable[None]]


def auto_view(s):
    """The user's settings with only 1xBit + Polymarket switched on."""
    return replace(s, disabled=[b for b in s.mode_bookies if b not in PAIR])


def leg_ref(arb: Arb, leg) -> dict | None:
    ev = arb.event_for(leg.bookie)
    return ev.bet_ref.get((leg.market, leg.outcome)) if ev else None


def auto_ok(arb: Arb, s, now: datetime | None = None) -> str | None:
    """Why this arb can't be played automatically (None = it can)."""
    now = now or datetime.now(timezone.utc)
    if len(arb.legs) != 2 or {l.bookie for l in arb.legs} != set(PAIR):
        return "nije par 1xBit + Polymarket"
    if arb.suspicious:
        return "sumnjivo velik profit"
    if any(not leg_ref(arb, l) for l in arb.legs):
        return "nema podataka za uplatu"
    if arb.event.start < now + timedelta(seconds=MIN_LEAD):
        return "meč uskoro počinje"
    if s.auto_hours and arb.event.start > now + timedelta(hours=s.auto_hours):
        return "meč počinje kasnije od pravila"
    if arb.profit_pct < s.auto_min:
        return "profit ispod pravila"
    return None


class AutoTrader:
    def __init__(self, service, store, send: Send, book: bk.Book | None = None,
                 poly: PolyExecutor | None = None, onexbit: OneXBitExecutor | None = None,
                 tz=None) -> None:
        self.service, self.store, self.send = service, store, send
        self.book = book or bk.Book()
        self.poly = poly or PolyExecutor()
        self.onexbit = onexbit or OneXBitExecutor()
        self.tz = tz
        self.halted = False  # 🛑: nothing new goes in until auto is switched on again
        self._lock = asyncio.Lock()  # one arb at a time (balances!)
        self._task: asyncio.Task | None = None
        self.tried: dict[str, tuple[float, tuple]] = {}  # arb key -> (when, legs)
        self.pending: dict[str, tuple[int, str, float]] = {}  # ✋ button id -> (uid, arb key, valid until)
        self.asked: set[int] = set()  # runs the user was asked to settle by hand
        self.reported: dict[int, str] = {}  # uid -> date of the last evening report
        self.geo_warned = False
        self._poly_tried = 0.0  # last Polymarket login attempt that failed

    # ---- after every scan

    def on_scan(self, allowed: Callable[[int], bool]) -> None:
        if self._task and not self._task.done():
            return
        users = [(uid, s) for uid, s in list(self.store.users.items())
                 if s.mode == "crypto" and allowed(uid) and (s.auto or self.book.open_runs(uid))]
        if users:
            self._task = asyncio.create_task(self._round(users))

    async def _round(self, users) -> None:
        for uid, s in users:
            try:
                await self.settle(uid)
                if s.auto and not self.halted and self.service.covers(s):
                    await self._play(uid, s)
                await self._report(uid)
            except Exception:
                log.exception("auto round %s failed", uid)

    def candidates(self, s) -> list[Arb]:
        now = time.time()
        busy = {r["arb_key"] for uid in self.store.users for r in self.book.open_runs(uid)}
        out = []
        for a in self.service.arbs_for(auto_view(s)):
            k = _key(a)
            if k in busy or any(p[1] == k for p in self.pending.values()) or auto_ok(a, s):
                continue
            prev = self.tried.get(k)
            if prev and prev[1] == _sig(a) and now - prev[0] < RETRY_SAME:
                continue
            out.append(a)
        return out

    async def _play(self, uid: int, s) -> None:
        for a in self.candidates(s)[:PER_ROUND]:
            if self.halted or not s.auto:
                return
            self.tried[_key(a)] = (time.time(), _sig(a))
            if s.auto_confirm:
                await self._ask(uid, s, a)
            else:
                text, markup = await self.execute(uid, s, a)
                if text:
                    await self.send(uid, text, markup)

    async def _ask(self, uid: int, s, a: Arb) -> None:
        from arb.tg.formatting import money

        token = pyrandom.token_hex(4)
        self.pending = {k: v for k, v in self.pending.items() if v[2] > time.time()}
        self.pending[token] = (uid, _key(a), time.time() + CONFIRM_SECONDS)
        legs = "\n".join(f"• {l.bookie}: {_label(a, l)} @ {l.odd:g}" for l in a.legs)
        await self.send(uid, f"✋ <b>Da uplatim?</b> {_esc(a.event.home)} – {_esc(a.event.away)}\n"
                             f"📊 {_esc(market_label(a.market, a.event.sport, a.event.home, a.event.away))} · "
                             f"profit {a.profit_pct:.2f}% · ulog do {money(s.auto_stake, '$')} $\n{legs}\n\n"
                             f"Dugme važi {CONFIRM_SECONDS} s (kvote se proveravaju ponovo pre uplate).",
                        ("confirm", token))

    async def confirm(self, uid: int, token: str):
        """✋ ✅ Uplati pressed: (text, markup)."""
        p = self.pending.pop(token, None)
        if not p or p[0] != uid:
            return "⌛ Ovo dugme više ne važi.", None
        if p[2] < time.time():
            return "⌛ Prošlo je više od 90 s – kvote su verovatno drugačije. Čekam sledeću.", None
        s = self.store.get(uid)
        arb = self.service.lookup(p[1], auto_view(s))
        if not arb:
            return "❌ Arbitraža je u međuvremenu nestala – ništa nije uplaćeno.", None
        return await self.execute(uid, s, arb, manual=True)

    # ---- limits and money

    def day_start(self) -> float:
        now = datetime.now(self.tz) if self.tz else datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def room(self, uid: int, s) -> tuple[float, str]:
        """(most a new arb may take in total, why it's 0)."""
        day_left = s.auto_daily - self.book.staked_since(uid, self.day_start())
        open_left = s.auto_open - self.book.exposure(uid)
        room = min(s.auto_stake, day_left, open_left)
        why = ("dnevni limit je potrošen" if day_left < AUTO_MIN_STAKE else
               "dostignut je limit otvorenih uloga" if open_left < AUTO_MIN_STAKE else "")
        return room, why

    async def ensure_poly(self, s) -> bool:
        """Connected to Polymarket (logs in with the stored key after a restart)."""
        if self.poly.connected:
            return True
        if not s.poly_funder or not self.poly.has_key() or time.time() - self._poly_tried < RECONNECT_WAIT:
            return False
        self._poly_tried = time.time()
        ok, msg = await self.poly.connect(s.poly_funder, s.poly_sig)
        if not ok:
            log.warning("Polymarket reconnect: %s", msg)
            self.poly.error = msg
        return ok

    async def balances(self, uid: int, s) -> dict[str, float | None]:
        x = self.book.balance(uid, "1xBit")
        pm = await self.poly.balance() if await self.ensure_poly(s) else None
        return {"1xBit": x.now if x.set_amount is not None else None, "Polymarket": pm}

    # ---- one arb

    async def execute(self, uid: int, s, arb: Arb, dry: bool = False, manual: bool = False):
        """Play this arb for real (or just build every order: dry). Returns (text for the user, markup)."""
        async with self._lock:
            return await self._execute(uid, s, arb, dry, manual)

    async def _execute(self, uid: int, s, arb: Arb, dry: bool, manual: bool):
        from arb.tg.formatting import money

        head = f"{'🔍 <b>Proba (ništa se ne šalje)</b>' if dry else '🤖 <b>Auto</b>'} · {_esc(arb.event.home)} – {_esc(arb.event.away)}"
        if not dry and (self.halted or not s.auto):
            return (f"{head}\n🛑 Automatsko klađenje je zaustavljeno – ništa nije uplaćeno.", None) if manual else (None, None)
        if not await self.ensure_poly(s):
            return f"{head}\n❌ Polymarket nije povezan (🤖 Auto → 🟣 Poveži Polymarket).", None
        if not self.onexbit.template():
            return f"{head}\n❌ 1xBit uplata još nije naučena (🤖 Auto → 🎓 Nauči 1xBit uplatu).", None
        x_bal = self.book.balance(uid, "1xBit")
        if x_bal.set_amount is None:
            return f"{head}\n❌ Upiši koliko imaš na 1xBit-u (🤖 Auto → ✏️ 1xBit balans).", None
        room, why = self.room(uid, s)
        if not dry and room < AUTO_MIN_STAKE:
            return (f"{head}\n⏸ {why} – čekam.", None) if manual else (None, None)
        room = max(room, AUTO_MIN_STAKE) if dry else room
        blocked, country = await self.poly.geoblock()
        if blocked is None or blocked:
            text = (f"{head}\n🌍 Polymarket ne dozvoljava klađenje odavde ({country}) – ništa nije uplaćeno."
                    if blocked else f"{head}\n🌍 Ne mogu da proverim da li Polymarket dozvoljava ovu zemlju – "
                                    "ništa nije uplaćeno.")
            if manual or dry or not self.geo_warned:
                self.geo_warned = True
                return text, None
            return None, None
        self.geo_warned = False

        # odds right now, from both bookies
        view = auto_view(s)
        fresh = [a for a in await self.service.verify([arb], view) if a.market == arb.market]
        a = fresh[0] if fresh else None
        if a is None or a.checked_at == 0:
            return (f"{head}\n❌ Kvote su se promenile – arbitraža više ne postoji. Ništa nije uplaćeno.", None) \
                if manual or dry else (None, None)
        if reason := auto_ok(a, s):
            return (f"{head}\n❌ {reason} – ništa nije uplaćeno.", None) if manual or dry else (None, None)
        pm_bal = await self.poly.balance(force=True)
        caps = {"1xBit": x_bal.now, "Polymarket": pm_bal or 0.0}
        stake = fit_stake(a, room, "$", caps, AUTO_MIN_STAKE)
        rows = a.plan(stake, "$") if stake else None
        if not rows or a.pct_for(stake, "$") < s.auto_min:
            return (f"{head}\n💼 Nema dovoljno novca (1xBit ~{money(max(x_bal.now, 0), '$')} $, Polymarket "
                    f"{money(pm_bal or 0, '$')} $) ili ponuda ne prima ni {AUTO_MIN_STAKE:g} $ – ništa nije uplaćeno.",
                    None) if manual or dry else (None, None)
        (l1, st1, _), (l2, st2, _) = sorted(rows, key=lambda r: r[0].bookie != "1xBit")  # 1xBit first
        if st2 < MIN_ORDER:  # Polymarket wouldn't take its leg: don't even start with 1xBit
            return (f"{head}\n💼 Polymarket deo bi bio {money(st2, '$')} $, a Polymarket prima od {MIN_ORDER:g} $ – "
                    "ništa nije uplaćeno.", None) if manual or dry else (None, None)
        ref1, ref2 = leg_ref(a, l1), leg_ref(a, l2)
        total = st1 + st2
        ev = a.event
        run = self.book.new_run(uid, _key(a), f"{ev.home} – {ev.away}", market_label(a.market, ev.sport, ev.home, ev.away),
                                ev.sport, ev.start.isoformat(), dry)

        # 1) 1xBit
        f1 = await self.onexbit.place(ref1, l1.odd, st1, dry)
        if not f1.ok:
            if f1.unknown:  # maybe in, maybe not: the user checks
                self.book.add_bet(run, uid, l1.bookie, l1.market, l1.outcome, _label(a, l1), st1, l1.odd, "",
                                  bk.PLACED, ref1, f1.error)
                self.book.add_bet(run, uid, l2.bookie, l2.market, l2.outcome, _label(a, l2), st2, l2.odd, "",
                                  bk.REFUSED, ref2, "nije uplaćeno – 1xBit nije potvrdio")
                self.book.finish_run(run, bk.EXPOSED, UNCONFIRMED)
                return (f"{head}\n⚠️ <b>{_esc(f1.error)}</b>\nAko je tiket prošao: 🛟 Pokrij (uplati Polymarket). "
                        f"Ako nije: ❌ Nije prošao.", ("unconfirmed", run))
            self.book.add_bet(run, uid, l1.bookie, l1.market, l1.outcome, _label(a, l1), st1, l1.odd, "",
                              bk.REFUSED, ref1, f1.error)
            self.book.finish_run(run, bk.FAILED if not dry else bk.DRY, f1.error)
            return f"{head}\n❌ 1xBit nije primio tiket: {_esc(f1.error)}\nNišta nije uplaćeno.", None
        self.book.add_bet(run, uid, l1.bookie, l1.market, l1.outcome, _label(a, l1), f1.stake, f1.odd,
                          f1.order_id, bk.DRY_BET if dry else bk.PLACED, ref1)

        # 2) Polymarket, all or nothing, not below break-even
        f2 = await self._poly(ref2, st2, total, dry)
        covered_at_loss = False
        if not f2.ok and not dry:
            f2 = await self._poly(ref2, st2, total * (1 - COVER_LOSS), dry, tries=1)
            covered_at_loss = f2.ok
        if not f2.ok:
            self.book.add_bet(run, uid, l2.bookie, l2.market, l2.outcome, _label(a, l2), st2, l2.odd, "",
                              bk.REFUSED, ref2, f2.error)
            self.book.finish_run(run, bk.EXPOSED if not dry else bk.DRY, f"Polymarket: {f2.error}")
            if dry:
                return f"{head}\n1xBit ✅ (proba)\n❌ Polymarket: {_esc(f2.error)}", None
            return (f"{head}\n🚨 <b>1xBit je uplaćen ({money(f1.stake, '$')} $ @ {f1.odd:.2f}), a Polymarket NIJE</b>: "
                    f"{_esc(f2.error)}\nOpklada je otvorena sama. 🛟 Pokrij pokušava ponovo (gubitak do 10%), "
                    f"ili je pokrij ručno na Polymarket-u: {_esc(l2_how(a, l2))}.", ("exposed", run))
        self.book.add_bet(run, uid, l2.bookie, l2.market, l2.outcome, _label(a, l2), f2.stake, f2.odd,
                          f2.order_id, bk.DRY_BET if dry else bk.PLACED, ref2)
        self.book.finish_run(run, bk.DRY if dry else bk.OPEN, "pokriveno uz mali gubitak" if covered_at_loss else "")
        r = self.book.run(run)
        return ticket_text(r, head), (None if dry else ("ticket", run))

    async def _poly(self, ref: dict, amount: float, need: float, dry: bool, tries: int = RETRIES) -> Fill:
        last = Fill(False, error="nema cene na kojoj se ovo isplati")
        for i in range(tries):
            worst = worst_price(amount, need, ref.get("rate", 0.0), ref.get("tick", "0.01"))
            if worst is None:
                return last
            last = await self.poly.buy(ref["token"], amount, worst, ref.get("tick", "0.01"),
                                       bool(ref.get("neg_risk")), ref.get("rate", 0.0), dry)
            if last.ok or dry:
                return last
            if i + 1 < tries:
                await asyncio.sleep(RETRY_WAIT)
        return last

    # ---- an open single bet

    async def cover(self, uid: int, run_id: int, loss: float = 0.10):
        """🛟 the uncovered Polymarket leg again, accepting up to `loss` of the total."""
        async with self._lock:
            r = self.book.run(run_id)
            if not r or r["uid"] != uid or r["status"] != bk.EXPOSED:
                return "Ovaj tiket više nije otvoren sam.", None
            todo = next((b for b in r["bets"] if b["status"] == bk.REFUSED and b["bookie"] == "Polymarket"), None)
            if not todo:
                return "Nemam šta da pokrijem na Polymarket-u.", None
            s = self.store.get(uid)
            if not await self.ensure_poly(s):
                return "❌ Polymarket nije povezan.", None
            blocked, country = await self.poly.geoblock()
            if blocked is not False:
                return f"🌍 Polymarket trenutno ne dozvoljava klađenje ({country or '?'}).", ("exposed", run_id)
            placed = sum(b["stake"] for b in r["bets"] if b["status"] == bk.PLACED)
            total = placed + todo["stake"]
            f = await self._poly(todo["ref"], todo["stake"], total * (1 - loss), False, tries=2)
            if not f.ok:
                return f"❌ Ni sad ne ide: {_esc(f.error)}. Pokrij ručno na Polymarket-u.", ("exposed", run_id)
            self.book.update_bet(todo["id"], bk.PLACED, f.stake, f.odd, f.order_id)
            self.book.finish_run(run_id, bk.OPEN, "pokriveno naknadno")
            return ticket_text(self.book.run(run_id), "🛟 <b>Pokriveno</b>"), ("ticket", run_id)

    def not_placed(self, uid: int, run_id: int) -> str:
        """❌ the unconfirmed 1xBit ticket didn't go through: nothing is open."""
        r = self.book.run(run_id)
        if not r or r["uid"] != uid or r["status"] != bk.EXPOSED or r["note"] != UNCONFIRMED:
            return "Ovaj tiket više nije otvoren (ili je 1xBit tiket potvrđen)."
        for b in r["bets"]:
            if b["status"] == bk.PLACED:
                self.book.update_bet(b["id"], bk.REFUSED, b["stake"], b["odd"], b["order_id"])
        self.book.finish_run(run_id, bk.FAILED, "1xBit tiket nije prošao (potvrđeno ručno)")
        return "✅ Zabeleženo: tiket nije prošao, ništa nije uplaćeno."

    # ---- results

    async def settle(self, uid: int) -> None:
        now = time.time()
        for r in self.book.open_runs(uid):
            try:
                start = datetime.fromisoformat(r["start"]).timestamp()
            except (TypeError, ValueError):
                continue
            if now < start + SETTLE_AFTER:
                continue
            pm = next((b for b in r["bets"] if b["bookie"] == "Polymarket" and b["ref"].get("token")), None)
            won = await resolution(pm["ref"]["condition"], pm["ref"]["token"]) if pm and pm["ref"].get(
                "condition") else None
            if won is None:
                if now > start + ASK_AFTER and r["id"] not in self.asked:
                    self.asked.add(r["id"])
                    await self.send(uid, f"❔ Ne znam ishod za #{r['id']} {_esc(r['name'])} – ko je dobio?",
                                    ("settle", r["id"]))
                continue
            if won:  # the Polymarket side won (if it was never placed, nothing of ours won)
                winner = pm["id"] if pm["status"] == bk.PLACED else -1
            else:  # the Polymarket side lost: the other side won
                others = [b for b in r["bets"] if b["status"] == bk.PLACED and b["id"] != pm["id"]]
                winner = others[0]["id"] if others else -1
            profit = self.book.settle(r["id"], winner)
            await self.send(uid, settled_text(self.book.run(r["id"]), profit), None)

    async def settle_by_hand(self, uid: int, run_id: int, bet_id: int | None) -> str:
        r = self.book.run(run_id)
        if not r or r["uid"] != uid:
            return "Nema tog tiketa."
        if r["status"] not in (bk.OPEN, bk.EXPOSED):
            return "Taj tiket je već zatvoren."
        profit = self.book.settle(run_id, bet_id)
        return settled_text(self.book.run(run_id), profit)

    async def _report(self, uid: int) -> None:
        now = datetime.now(self.tz) if self.tz else datetime.now()
        if now.hour < REPORT_HOUR or self.reported.get(uid) == now.date().isoformat():
            return
        self.reported[uid] = now.date().isoformat()
        day = self.book.day(uid, self.day_start())
        if day["runs"] or day["settled_n"]:
            await self.send(uid, report_text(day, self.book.balance(uid, "1xBit"),
                                             await self.poly.balance() if self.poly.connected else None), None)

    async def stop(self) -> None:
        self.halted = True
        self.pending.clear()

    async def close(self) -> None:
        await self.onexbit.close()


def l2_how(a: Arb, leg) -> str:
    ev = a.event_for(leg.bookie)
    how = ev.how.get((leg.market, leg.outcome)) if ev else None
    return how or _label(a, leg)


def _label(a: Arb, leg) -> str:
    from arb.tg.formatting import outcome_text

    return outcome_text(a, leg.outcome)


def _key(a: Arb) -> str:
    from arb.tg.service import arb_key

    return arb_key(a)


def _sig(a: Arb) -> tuple:
    return tuple((l.bookie, l.market, l.outcome, l.odd) for l in a.legs)


def _esc(x) -> str:
    from html import escape

    return escape(str(x))


# ---- messages

_STATUS = {bk.OPEN: "⏳ čeka meč", bk.EXPOSED: "🚨 otvoreno samo jedno", bk.FAILED: "❌ ništa uplaćeno",
           bk.SETTLED: "🏁 završeno", bk.DRY: "🔍 proba", bk.PLACING: "⏳ uplata u toku"}
_BET = {bk.PLACED: "✅", bk.REFUSED: "❌", bk.WON: "🏆", bk.LOST: "▫️", bk.VOID: "↩️", bk.DRY_BET: "🔍"}


def ticket_text(r: dict, head: str = "") -> str:
    from arb.tg.formatting import money, signed

    lines = [head or f"🎫 <b>#{r['id']}</b> {_esc(r['name'])}"]
    if head:
        lines.append(f"🎫 #{r['id']} {_esc(r['name'])}")
    lines.append(f"📊 {_esc(r['market'])} · {_STATUS.get(r['status'], r['status'])}")
    for b in r["bets"]:
        line = (f"{_BET.get(b['status'], '')} {b['bookie']}: <b>{b['label']}</b> @ {b['odd']:.3f} × "
                f"{money(b['stake'], '$')} $")
        if b["order_id"] and b["order_id"] != "proba":
            line += f" · ID <code>{_esc(b['order_id'][:20])}</code>"
        if b["error"]:
            line += f"\n   ↳ {_esc(b['error'])}"
        lines.append(line)
    if r["status"] == bk.SETTLED:
        lines.append(f"💰 zarada <b>{signed(r['profit'] or 0, '$')} $</b> (dobio {r['winner'] or '—'})")
    elif r["status"] in (bk.OPEN, bk.DRY):
        pct = r["planned_profit"] / r["total"] * 100 if r["total"] else 0
        lines.append(f"💰 ukupno {money(r['total'], '$')} $ → sigurna zarada <b>{signed(r['planned_profit'], '$')} $</b>"
                     f" ({pct:+.2f}%)")
    if r["note"]:
        lines.append(f"📝 {_esc(r['note'])}")
    return "\n".join(lines)


def settled_text(r: dict, profit: float) -> str:
    from arb.tg.formatting import signed

    who = r["winner"] or ("poništeno, ulozi vraćeni" if abs(profit) < 0.005 else "nijedna naša opklada")
    return (f"🏁 <b>Meč završen</b> · #{r['id']} {_esc(r['name'])}\n"
            f"Dobio: <b>{who}</b> · zarada <b>{signed(profit, '$')} $</b>"
            + ("\n💡 Polymarket dobitak preuzmi na sajtu (Claim / Redeem)." if r["winner"] == "Polymarket" else ""))


def report_text(day: dict, x_bal: bk.Balance, pm_bal: float | None) -> str:
    from arb.tg.formatting import money, signed

    lines = ["📒 <b>Prave uplate – danas</b>", "",
             f"🎫 arbitraža: <b>{day['runs']}</b> (odbijeno pre uplate: {day['failed']}, otvoreno samo jedno: "
             f"{day['exposed']})",
             f"💵 uloženo danas: <b>{money(day['staked'], '$')} $</b>",
             f"🏁 završeno: {day['settled_n']} · zarada <b>{signed(day['settled_profit'], '$')} $</b>",
             f"⏳ u igri: {money(day['open'], '$')} $ · čeka zarada {signed(day['pending_profit'], '$')} $", "",
             "💼 <b>Balans</b>",
             f"🔵 1xBit: ~{money(x_bal.now, '$')} $" if x_bal.set_amount is not None else "🔵 1xBit: nije upisan",
             f"🟣 Polymarket: {money(pm_bal, '$')} $" if pm_bal is not None else "🟣 Polymarket: nije povezan"]
    return "\n".join(lines)
