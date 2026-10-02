"""The ticket book (knjiga tiketa) for real bets: data/live.db.

run   = one arbitrage played for real (its legs are bets)
bet   = one ticket on one exchange: stake, odd, the exchange's id of it, result
seen  = every SX Bet + Polymarket arb the scans found (arb.live.stats: how many there are)

Test runs ("🔍 Proba", dry = 1) are stored too, but never count in balances or limits."""
from __future__ import annotations

import json
import sqlite3
import time

from arb.config import DATA_DIR

DB_FILE = DATA_DIR / "live.db"

# run statuses
PLACING = "placing"  # legs are going in right now
OPEN = "open"  # every leg placed, waiting for the match
EXPOSED = "exposed"  # a leg is in, but the other couldn't be covered - one bet is open alone
FAILED = "failed"  # nothing was placed (first leg refused) - no money moved
SETTLED = "settled"  # the match is over, profit known
DRY = "dry"  # 🔍 test run, nothing sent

# bet statuses
PLACED, REFUSED, WON, LOST, VOID, DRY_BET = "placed", "refused", "won", "lost", "void", "dry"
LIVE_BETS = (PLACED, WON, LOST, VOID)  # real money left the account


def _db(path=None) -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(path or DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, uid INTEGER, arb_key TEXT, name TEXT, market TEXT,
        sport TEXT, start TEXT, status TEXT, total REAL DEFAULT 0, planned_profit REAL DEFAULT 0,
        profit REAL, settled_at REAL, winner TEXT, note TEXT DEFAULT '', dry INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, at REAL, uid INTEGER, bookie TEXT, market TEXT,
        outcome TEXT, label TEXT, stake REAL, odd REAL, payout REAL, order_id TEXT, status TEXT,
        ref TEXT, error TEXT DEFAULT '', settled_at REAL);
    CREATE TABLE IF NOT EXISTS seen (
        arb_key TEXT, sig TEXT, first_at REAL, last_at REAL, sport TEXT, name TEXT, market TEXT, start TEXT,
        pct REAL, cap REAL, PRIMARY KEY (arb_key, sig));
    """)
    return con


class Book:
    def __init__(self, path=None) -> None:
        self.path = path

    def _con(self) -> sqlite3.Connection:
        return _db(self.path)

    # ---- writing

    def new_run(self, uid: int, arb_key: str, name: str, market: str, sport: str, start: str,
                dry: bool = False) -> int:
        with self._con() as con:
            cur = con.execute("INSERT INTO runs (at, uid, arb_key, name, market, sport, start, status, dry) "
                              "VALUES (?,?,?,?,?,?,?,?,?)",
                              (time.time(), uid, arb_key, name, market, sport, start, DRY if dry else PLACING,
                               int(dry)))
            return cur.lastrowid

    def add_bet(self, run_id: int, uid: int, bookie: str, market: str, outcome: str, label: str, stake: float,
                odd: float, order_id: str, status: str, ref: dict | None = None, error: str = "") -> int:
        with self._con() as con:
            cur = con.execute(
                "INSERT INTO bets (run_id, at, uid, bookie, market, outcome, label, stake, odd, payout, order_id, "
                "status, ref, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), uid, bookie, market, outcome, label, stake, odd, stake * odd, order_id,
                 status, json.dumps(ref or {}), error))
            return cur.lastrowid

    def update_bet(self, bet_id: int, status: str, stake: float, odd: float, order_id: str, error: str = "") -> None:
        with self._con() as con:
            con.execute("UPDATE bets SET status = ?, stake = ?, odd = ?, payout = ?, order_id = ?, error = ? "
                        "WHERE id = ?", (status, stake, odd, stake * odd, order_id, error, bet_id))

    def finish_run(self, run_id: int, status: str, note: str = "") -> None:
        """Totals come from the bets that really went in."""
        with self._con() as con:
            bets = con.execute("SELECT * FROM bets WHERE run_id = ? AND status IN (?, ?)",
                               (run_id, PLACED, DRY_BET)).fetchall()
            total = sum(b["stake"] for b in bets)
            planned = (min(b["payout"] for b in bets) - total) if bets else 0.0
            if status == EXPOSED:
                planned = -total  # worst case: the uncovered side wins
            con.execute("UPDATE runs SET status = ?, total = ?, planned_profit = ?, note = ? WHERE id = ?",
                        (status, total, planned, note, run_id))

    def settle(self, run_id: int, winner_bet: int | None, note: str = "") -> float:
        """The match is over: `winner_bet` won (None = every bet void, stakes back; -1 = none of
        ours won - an uncovered bet lost). Returns the profit."""
        now = time.time()
        with self._con() as con:
            bets = con.execute("SELECT * FROM bets WHERE run_id = ? AND status IN (?, ?, ?, ?)",
                               (run_id, PLACED, WON, LOST, VOID)).fetchall()
            total = sum(b["stake"] for b in bets)
            back = 0.0
            for b in bets:
                if winner_bet is None:
                    st, back = VOID, back + b["stake"]
                elif b["id"] == winner_bet:
                    st, back = WON, back + b["payout"]
                else:
                    st = LOST
                con.execute("UPDATE bets SET status = ?, settled_at = ? WHERE id = ?", (st, now, b["id"]))
            winner = next((b["bookie"] for b in bets if b["id"] == winner_bet), "")
            profit = back - total
            con.execute("UPDATE runs SET status = ?, profit = ?, settled_at = ?, winner = ?, "
                        "note = CASE WHEN ? = '' THEN note ELSE ? END WHERE id = ?",
                        (SETTLED, profit, now, winner, note, note, run_id))
        return profit

    def record_seen(self, rows: list[tuple]) -> None:
        """One scan's SX Bet + Polymarket arbs: (arb key, legs signature, sport, name, market, start,
        profit %, $ it takes). The same arb with the same odds is one row; first and last seen kept."""
        now = time.time()
        with self._con() as con:
            con.executemany(
                "INSERT INTO seen VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (arb_key, sig) DO UPDATE SET "
                "last_at = excluded.last_at, pct = MAX(pct, excluded.pct), cap = MAX(cap, excluded.cap)",
                [(k, sig, now, now, sport, name, market, start, pct, cap)
                 for k, sig, sport, name, market, start, pct, cap in rows])

    def seen_since(self, since: float) -> list[dict]:
        with self._con() as con:
            return [dict(r) for r in con.execute("SELECT * FROM seen WHERE last_at >= ? ORDER BY first_at",
                                                 (since,)).fetchall()]

    def seen_first(self) -> float | None:
        with self._con() as con:
            r = con.execute("SELECT MIN(first_at) FROM seen").fetchone()
        return r[0]

    # ---- reading

    def run(self, run_id: int) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not r:
                return None
            bets = con.execute("SELECT * FROM bets WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return dict(r) | {"bets": [dict(b) | {"ref": json.loads(b["ref"] or "{}")} for b in bets]}

    def runs(self, uid: int, since: float = 0.0, statuses: tuple[str, ...] | None = None,
             limit: int = 1000, real_only: bool = True) -> list[dict]:
        q = "SELECT id FROM runs WHERE uid = ? AND at >= ?"
        args: list = [uid, since]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if real_only:
            q += " AND dry = 0"
        q += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        with self._con() as con:
            ids = [r["id"] for r in con.execute(q, args).fetchall()]
        return [self.run(i) for i in ids]

    def open_runs(self, uid: int) -> list[dict]:
        return self.runs(uid, statuses=(OPEN, EXPOSED))

    def exposure(self, uid: int) -> float:
        """Money in bets whose match isn't settled yet."""
        with self._con() as con:
            r = con.execute("SELECT COALESCE(SUM(stake), 0) FROM bets WHERE uid = ? AND status = ?",
                            (uid, PLACED)).fetchone()
        return float(r[0])

    def staked_since(self, uid: int, since: float) -> float:
        with self._con() as con:
            r = con.execute(f"SELECT COALESCE(SUM(stake), 0) FROM bets WHERE uid = ? AND at >= ? "
                            f"AND status IN ({','.join('?' * len(LIVE_BETS))})", (uid, since, *LIVE_BETS)).fetchone()
        return float(r[0])

    def day(self, uid: int, since: float) -> dict:
        """Totals for the report: tickets placed since `since` and runs settled since then."""
        placed = self.runs(uid, since)
        with self._con() as con:
            settled = con.execute("SELECT COUNT(*), COALESCE(SUM(profit), 0) FROM runs WHERE uid = ? AND dry = 0 "
                                  "AND status = ? AND settled_at >= ?", (uid, SETTLED, since)).fetchone()
        return {
            "runs": len([r for r in placed if r["status"] != FAILED]),
            "failed": len([r for r in placed if r["status"] == FAILED]),
            "exposed": len([r for r in placed if r["status"] == EXPOSED]),
            "staked": self.staked_since(uid, since),
            "settled_n": settled[0], "settled_profit": float(settled[1]),
            "open": self.exposure(uid),
            "pending_profit": sum(r["planned_profit"] for r in self.open_runs(uid)),
        }

