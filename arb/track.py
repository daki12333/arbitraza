"""📒 Tickets and money the user plays by hand, on any bookie: data/tracker.db.

The bot places nothing here. The user tells it what they played ("✍️ Odigrao sam" on an arb,
or a ticket typed in by hand) and what they paid in, took out or moved between bookies;
the bot keeps the balance of every bookie, the tickets in play, the results and the stats.

ticket = one arb (or any bet) the user played, its legs are the bets on each bookie
move   = one change of money on one bookie: the balance of a bookie is the sum of its moves

    deposit / withdraw   money paid in / taken out
    move_out / move_in   sent from one bookie to another (what arrived can be less: fees)
    fix                  "✏️ Ispravi": the real balance differed, the difference is booked
    stake / payout       a ticket's leg went in / paid back (on settling)
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

from arb.config import DATA_DIR

DB_FILE = DATA_DIR / "tracker.db"

OPEN, SETTLED, DELETED = "open", "settled", "deleted"
# leg results: what one bet paid back, per stake and odd
WON, LOST, VOID, HALF_WON, HALF_LOST = "won", "lost", "void", "half_won", "half_lost"
RESULTS = (WON, LOST, VOID, HALF_WON, HALF_LOST)
FINISH_AFTER = 3 * 3600  # s after kickoff: a match is surely over, ask how it went


def leg_payout(stake: float, odd: float, result: str) -> float:
    return {WON: stake * odd, LOST: 0.0, VOID: stake,
            HALF_WON: stake / 2 * odd + stake / 2, HALF_LOST: stake / 2}[result]


def _db(path=None) -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(path or DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, at REAL, arb_key TEXT DEFAULT '', name TEXT,
        sport TEXT DEFAULT '', market TEXT DEFAULT '', start REAL, currency TEXT, planned REAL DEFAULT 0,
        status TEXT, profit REAL, settled_at REAL, reminded INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, bookie TEXT, outcome TEXT, label TEXT,
        odd REAL, stake REAL, result TEXT DEFAULT '', payout REAL);
    CREATE TABLE IF NOT EXISTS moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, at REAL, bookie TEXT, amount REAL, kind TEXT,
        ticket_id INTEGER, note TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS moves_uid ON moves (uid, bookie);
    CREATE INDEX IF NOT EXISTS tickets_uid ON tickets (uid, status);
    """)
    return con


class Tracker:
    def __init__(self, path=None) -> None:
        self.path = path

    def _con(self) -> sqlite3.Connection:
        return _db(self.path)

    # ---- money

    def _move(self, con, uid: int, bookie: str, amount: float, kind: str, ticket_id: int | None = None,
              note: str = "") -> None:
        con.execute("INSERT INTO moves (uid, at, bookie, amount, kind, ticket_id, note) VALUES (?,?,?,?,?,?,?)",
                    (uid, time.time(), bookie, round(amount, 6), kind, ticket_id, note))

    def deposit(self, uid: int, bookie: str, amount: float, note: str = "") -> None:
        with self._con() as con:
            self._move(con, uid, bookie, amount, "deposit", note=note)

    def withdraw(self, uid: int, bookie: str, amount: float, note: str = "") -> None:
        with self._con() as con:
            self._move(con, uid, bookie, -amount, "withdraw", note=note)

    def transfer(self, uid: int, src: str, dst: str, sent: float, received: float | None = None) -> None:
        """`sent` leaves `src`, `received` (default: all of it) arrives on `dst` - the rest were fees."""
        received = sent if received is None else received
        with self._con() as con:
            self._move(con, uid, src, -sent, "move_out", note=dst)
            self._move(con, uid, dst, received, "move_in", note=src)

    def fix(self, uid: int, bookie: str, actual: float) -> float:
        """The bookie really shows `actual`: book the difference. Returns it (0 = it matched)."""
        diff = round(actual - self.balances(uid).get(bookie, 0.0), 6)
        if diff:
            with self._con() as con:
                self._move(con, uid, bookie, diff, "fix")
        return diff

    def balances(self, uid: int) -> dict[str, float]:
        """Money on each bookie by the book (what's free to bet - stakes in play are already out)."""
        with self._con() as con:
            rows = con.execute("SELECT bookie, SUM(amount) s FROM moves WHERE uid=? GROUP BY bookie", (uid,))
            return {r["bookie"]: round(r["s"], 6) for r in rows}

    def in_play(self, uid: int) -> dict[str, float]:
        """Stakes of open tickets on each bookie."""
        with self._con() as con:
            rows = con.execute("SELECT l.bookie, SUM(l.stake) s FROM legs l JOIN tickets t ON t.id = l.ticket_id "
                               "WHERE t.uid=? AND t.status=? GROUP BY l.bookie", (uid, OPEN))
            return {r["bookie"]: r["s"] for r in rows}

    def moves(self, uid: int, limit: int = 20, kinds: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM moves WHERE uid=?"
        args: list = [uid]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._con() as con:
            return con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()

    # ---- tickets

    def add_ticket(self, uid: int, name: str, legs: list[dict], currency: str, start: float | None = None,
                   sport: str = "", market: str = "", arb_key: str = "", planned: float = 0.0) -> int:
        """legs: [{"bookie", "outcome", "label", "odd", "stake"}]; every stake leaves its bookie."""
        with self._con() as con:
            tid = con.execute(
                "INSERT INTO tickets (uid, at, arb_key, name, sport, market, start, currency, planned, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, time.time(), arb_key, name, sport, market, start or time.time(), currency, planned, OPEN),
            ).lastrowid
            for l in legs:
                con.execute("INSERT INTO legs (ticket_id, bookie, outcome, label, odd, stake) VALUES (?,?,?,?,?,?)",
                            (tid, l["bookie"], l.get("outcome", ""), l.get("label", ""), l["odd"], l["stake"]))
                self._move(con, uid, l["bookie"], -l["stake"], "stake", tid)
            return tid

    def ticket(self, tid: int, uid: int | None = None) -> sqlite3.Row | None:
        with self._con() as con:
            t = con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if t is None or t["status"] == DELETED or (uid is not None and t["uid"] != uid):
            return None
        return t

    def legs(self, tid: int) -> list[sqlite3.Row]:
        with self._con() as con:
            return con.execute("SELECT * FROM legs WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()

    def tickets(self, uid: int, status: str = OPEN, limit: int = 50) -> list[sqlite3.Row]:
        order = "start, id" if status == OPEN else "settled_at DESC, id DESC"
        with self._con() as con:
            return con.execute(f"SELECT * FROM tickets WHERE uid=? AND status=? ORDER BY {order} LIMIT ?",
                               (uid, status, limit)).fetchall()

    def due(self, now: float | None = None) -> list[sqlite3.Row]:
        """Open tickets whose match is surely over and that weren't asked about yet."""
        now = now or time.time()
        with self._con() as con:
            return con.execute("SELECT * FROM tickets WHERE status=? AND reminded=0 AND start <= ? ORDER BY start",
                               (OPEN, now - FINISH_AFTER)).fetchall()

    def mark_reminded(self, tid: int) -> None:
        with self._con() as con:
            con.execute("UPDATE tickets SET reminded=1 WHERE id=?", (tid,))

    def set_result(self, tid: int, leg_id: int, result: str) -> None:
        """One leg's result while the ticket is still open ('' = not known yet)."""
        assert result in RESULTS or result == ""
        with self._con() as con:
            con.execute("UPDATE legs SET result=? WHERE id=? AND ticket_id=?", (result, leg_id, tid))

    def win(self, tid: int, leg_id: int) -> float:
        """The usual end of an arb: this leg won, every other one lost. Settles; returns the profit."""
        with self._con() as con:
            con.execute("UPDATE legs SET result=CASE WHEN id=? THEN ? ELSE ? END WHERE ticket_id=?",
                        (leg_id, WON, LOST, tid))
        return self.settle(tid)

    def settle(self, tid: int) -> float:
        """Every leg has a result: pay them back onto their bookies. Returns the ticket's profit."""
        t = self.ticket(tid)
        if t is None or t["status"] != OPEN:
            raise ValueError("ticket is not open")
        legs = self.legs(tid)
        if any(l["result"] not in RESULTS for l in legs):
            raise ValueError("a leg has no result")
        with self._con() as con:
            profit = 0.0
            for l in legs:
                pay = leg_payout(l["stake"], l["odd"], l["result"])
                profit += pay - l["stake"]
                con.execute("UPDATE legs SET payout=? WHERE id=?", (pay, l["id"]))
                if pay:
                    self._move(con, t["uid"], l["bookie"], pay, "payout", tid)
            con.execute("UPDATE tickets SET status=?, profit=?, settled_at=? WHERE id=?",
                        (SETTLED, round(profit, 6), time.time(), tid))
        return round(profit, 6)

    def reopen(self, tid: int) -> None:
        """Undo a wrong result: the payouts go back off the bookies, the ticket is open again."""
        with self._con() as con:
            con.execute("DELETE FROM moves WHERE ticket_id=? AND kind='payout'", (tid,))
            con.execute("UPDATE legs SET result='', payout=NULL WHERE ticket_id=?", (tid,))
            con.execute("UPDATE tickets SET status=?, profit=NULL, settled_at=NULL WHERE id=?", (OPEN, tid))

    def delete(self, tid: int) -> None:
        """A ticket entered by mistake: its stakes and payouts go back as if it never was."""
        with self._con() as con:
            con.execute("DELETE FROM moves WHERE ticket_id=?", (tid,))
            con.execute("UPDATE tickets SET status=? WHERE id=?", (DELETED, tid))

    # ---- stats

    def stats(self, uid: int, since: float = 0.0) -> dict[str, dict]:
        """Per currency: settled tickets since `since` - how many, turnover, profit, by bookie and pair."""
        with self._con() as con:
            tickets = con.execute("SELECT * FROM tickets WHERE uid=? AND status=? AND settled_at >= ?",
                                  (uid, SETTLED, since)).fetchall()
            legs = defaultdict(list)
            if tickets:
                ids = [t["id"] for t in tickets]
                for l in con.execute(f"SELECT * FROM legs WHERE ticket_id IN ({','.join('?' * len(ids))})", ids):
                    legs[l["ticket_id"]].append(l)
            open_rows = con.execute("SELECT currency, COUNT(*) n FROM tickets WHERE uid=? AND status=? "
                                    "GROUP BY currency", (uid, OPEN)).fetchall()
        out: dict[str, dict] = {}

        def cur(c: str) -> dict:
            return out.setdefault(c, {"tickets": 0, "staked": 0.0, "profit": 0.0, "planned": 0.0, "plus": 0,
                                      "minus": 0, "best": None, "worst": None, "open": 0,
                                      "bookies": defaultdict(lambda: {"legs": 0, "staked": 0.0, "net": 0.0}),
                                      "pairs": defaultdict(lambda: {"n": 0, "profit": 0.0})})
        for t in tickets:
            o = cur(t["currency"])
            ls = legs[t["id"]]
            o["tickets"] += 1
            o["staked"] += sum(l["stake"] for l in ls)
            o["profit"] += t["profit"]
            o["planned"] += t["planned"] or 0.0
            o["plus" if t["profit"] >= 0 else "minus"] += 1
            if o["best"] is None or t["profit"] > o["best"]["profit"]:
                o["best"] = t
            if o["worst"] is None or t["profit"] < o["worst"]["profit"]:
                o["worst"] = t
            for l in ls:
                b = o["bookies"][l["bookie"]]
                b["legs"] += 1
                b["staked"] += l["stake"]
                b["net"] += (l["payout"] or 0.0) - l["stake"]
            pair = " + ".join(sorted({l["bookie"] for l in ls}))
            o["pairs"][pair]["n"] += 1
            o["pairs"][pair]["profit"] += t["profit"]
        for r in open_rows:
            cur(r["currency"])["open"] = r["n"]
        return out
