"""Offline tests for real betting (arb.live) - no internet, no bookies, no Telegram:
python test_live.py

Fake 1xBit / Polymarket executors and a fake scan service drive the real engine, the
real ticket book (a temporary SQLite file) and the real 🤖 Auto buttons."""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import keyring
from keyring.backend import KeyringBackend

from arb.arbitrage import find_arbs
from arb.live import Fill, book as bk, engine as eng, onexbit, polymarket as pm, vault
from arb.matcher import _swap_sides
from arb.models import Event
from arb.tg.service import arb_key
from arb.tg.storage import UserSettings

UID = 7


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        if len(password) > 1280:
            raise ValueError("Windows would refuse this (too long)")
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError
        if (service, username) not in self.data:
            raise PasswordDeleteError(username)
        del self.data[(service, username)]


keyring.set_keyring(MemoryKeyring())


# ---------------------------------------------------------------- pieces

def test_worst_price():
    # 5 $ on Polymarket, the arb needs 10 $ back -> cost per share 0.5
    assert pm.worst_price(5, 10, 0.0, "0.01") == 0.5
    p = pm.worst_price(5, 10, 0.05, "0.01")
    assert p is not None and 5 / pm.cost(p, 0.05) >= 10 - 1e-9  # at the worst price it still returns enough
    assert 5 / pm.cost(p + 0.01, 0.05) < 10  # one tick higher it wouldn't
    p = pm.worst_price(4.2, 9.1, 0.05, "0.001")
    assert p is not None and 4.2 / pm.cost(p, 0.05) >= 9.1 - 1e-9 and 4.2 / pm.cost(p + 0.001, 0.05) < 9.1
    assert pm.worst_price(5, 4, 0.0) is None  # would need a price above 1
    assert pm.normalize_key("ab" * 32) == "0x" + "ab" * 32 and pm.normalize_key("0x123") is None


def test_parse_order():
    f = pm.parse_order({"success": True, "orderID": "0xabc", "status": "matched", "makingAmount": "5",
                        "takingAmount": "10.2"}, 5, 0.5, 0.0)
    assert f.ok and f.order_id == "0xabc" and abs(f.payout - 10.2) < 1e-9
    f = pm.parse_order({"success": False, "errorMsg": "order couldn't be fully filled. FOK orders are fully filled "
                                                      "or killed."}, 5, 0.5, 0.0)
    assert not f.ok and "FOK" in f.error


def test_vault():
    vault.put("x", "short")
    assert vault.get("x") == "short"
    long = json.dumps({"body": "y" * 5000})
    vault.put("x", long)  # split into pieces
    assert vault.get("x") == long
    vault.put("x", "again short")  # old pieces removed
    assert vault.get("x") == "again short"
    assert not any(k[1].startswith("x#") for k in keyring.get_keyring().data)
    vault.delete("x")
    assert vault.get("x") is None


SLIP = {"Events": [{"GameId": 111, "Type": 1, "Coef": 1.9, "Param": 0, "PlayerId": 0, "Kind": 1}],
        "Summ": "0.5", "Lng": "en", "UserId": 42, "Vid": 0, "hash": "secret-session", "CheckCf": 0}


def test_onexbit_template():
    assert onexbit.looks_like_bet(SLIP)
    assert not onexbit.looks_like_bet(SLIP | {"Summ": "0"})  # the "check the slip" call
    assert not onexbit.looks_like_bet({"Events": []})
    t = onexbit.make_template("https://1xbit.com/web-api/bet/make", {
        "content-type": "application/json", "cookie": "SESSION", "x-auth": "tok", ":authority": "1xbit.com",
        "sec-ch-ua": "x", "user-agent": "ua"}, SLIP)
    assert t["headers"] == {"content-type": "application/json", "x-auth": "tok"}
    body = onexbit.build_body(t, {"GameId": 999, "Type": 9, "Param": 2.5}, 2.137, 3.4)
    ev = body["Events"][0]
    assert (ev["GameId"], ev["Type"], ev["Coef"], ev["Param"]) == (999, 9, 2.137, 2.5)
    assert body["Summ"] == "3.4" and body["hash"] == "secret-session" and body["UserId"] == 42
    assert SLIP["Events"][0]["GameId"] == 111  # the template itself untouched
    ok, ticket, coef, err = onexbit.parse_response(200, json.dumps(
        {"Success": True, "Value": {"Id": 555, "Coef": 2.13}, "Error": ""}))
    assert ok and ticket == "555" and coef == 2.13
    ok, _, _, err = onexbit.parse_response(200, json.dumps({"Success": False, "Error": "Coefficient changed"}))
    assert not ok and "changed" in err
    ok, *_ = onexbit.parse_response(502, "<html>bad gateway</html>")
    assert not ok


def test_scraper_refs():
    from arb.scrapers.onexbet import OneXBitScraper
    from arb.scrapers.polymarket import PolymarketScraper

    sc = OneXBitScraper()
    g = {"I": 321, "O1": "Alpha", "O2": "Beta", "S": int(time.time()) + 7200, "L": "Liga", "LI": 5,
         "E": [{"G": 1, "T": 1, "C": 2.1}, {"G": 1, "T": 3, "C": 1.8}, {"G": 17, "T": 9, "C": 1.9, "P": 2.5},
               {"G": 17, "T": 10, "C": 1.95, "P": 2.5}]}
    ev = sc._parse(g, 4)  # tennis
    assert ev.bet_ref[("12", "1")] == {"GameId": 321, "Type": 1, "Param": 0, "Group": 1}
    assert ev.bet_ref[("OU_2.5", "U")]["Type"] == 10 and ev.bet_ref[("OU_2.5", "U")]["Param"] == 2.5
    _swap_sides(ev)  # the group has the players the other way round
    assert ev.bet_ref[("12", "2")]["Type"] == 1  # our "2" is still the site's "1"

    ps = PolymarketScraper()
    start = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market = {"sportsMarketType": "moneyline", "outcomes": '["Alpha", "Beta"]', "clobTokenIds": '["T1", "T2"]',
              "bestBid": 0.45, "bestAsk": 0.47, "conditionId": "0xcond", "negRisk": False,
              "orderPriceMinTickSize": 0.01, "question": "Alpha vs. Beta"}
    links = []
    ev = ps._parse({"id": 9, "title": "Alpha vs. Beta", "startTime": start, "slug": "a-b"}, [market],
                   {"name": "ATP"}, "tennis", datetime.now(timezone.utc), links)
    assert ev.bet_ref[("12", "1")]["token"] == "T1" and ev.bet_ref[("12", "2")]["token"] == "T2"
    assert ev.bet_ref[("12", "1")]["condition"] == "0xcond" and ev.bet_ref[("12", "1")]["tick"] == "0.01"


# ---------------------------------------------------------------- engine with fake bookies

def make_arb(x_odds=(2.1, 1.7), p_odds=(1.75, 2.0), hours=2.0):
    start = datetime.now(timezone.utc) + timedelta(hours=hours)
    x = Event("1xBit", "321", "tennis", "Alpha", "Beta", start, group="g1")
    x.markets["12"] = {"1": x_odds[0], "2": x_odds[1]}
    x.bet_ref = {("12", "1"): {"GameId": 321, "Type": 1, "Param": 0}, ("12", "2"): {"GameId": 321, "Type": 3, "Param": 0}}
    p = Event("Polymarket", "9", "tennis", "Alpha", "Beta", start, group="g1")
    p.markets["12"] = {"1": p_odds[0], "2": p_odds[1]}
    p.depth = {("12", "1"): [(p_odds[0], 500.0)], ("12", "2"): [(p_odds[1], 500.0)]}
    p.bet_ref = {("12", o): {"token": f"T{o}", "condition": "0xcond", "rate": 0.0, "neg_risk": False, "tick": "0.01"}
                 for o in ("1", "2")}
    arbs = find_arbs([[x, p]])
    assert arbs, "test arb not found"
    return arbs[0]


class FakeService:
    def __init__(self, arb):
        self.arb = arb

    def covers(self, s):
        return True

    async def fresh(self, s):
        return None

    def arbs_for(self, s):
        return [self.arb] if self.arb else []

    async def verify(self, arbs, s):
        if not self.arb:
            return []
        self.arb.checked_at = time.time()
        return [self.arb]

    def lookup(self, key, s):
        return self.arb if self.arb and arb_key(self.arb) == key else None


class FakePoly:
    name = "Polymarket"

    def __init__(self, fills=None, bal=50.0, blocked=False):
        self.fills = list(fills or [])
        self.bal, self.blocked = bal, blocked
        self.connected = True
        self.error = ""
        self.calls = []

    def has_key(self):
        return True

    async def connect(self, funder, sig, key=None):
        return True, "ok"

    async def balance(self, force=False):
        return self.bal

    async def geoblock(self):
        return self.blocked, "RS"

    async def buy(self, token, amount, worst, tick, neg_risk, rate, dry=False):
        self.calls.append((token, amount, worst, dry))
        if dry:
            return Fill(True, "proba", amount, amount / pm.cost(worst, rate), worst)
        f = self.fills.pop(0) if self.fills else "ok"
        if f == "ok":
            self.bal -= amount
            return Fill(True, f"0xord{len(self.calls)}", amount, amount / worst, worst)
        return Fill(False, error=f)


class FakeOneXBit:
    name = "1xBit"
    window_open = True

    def __init__(self, result="ok"):
        self.result = result
        self.calls = []

    def template(self):
        return {"learned_at": time.time()}

    async def place(self, ref, odd, stake, dry=False):
        self.calls.append((ref, odd, stake, dry))
        if self.result == "ok" or dry:
            return Fill(True, "T-1" if not dry else "proba", stake, stake * odd)
        if self.result == "unknown":
            return Fill(False, stake=stake, unknown=True, error="nije se javio")
        return Fill(False, error=self.result)

    async def open_window(self):
        return "ok"

    async def close(self):
        pass


def make_trader(arb, poly=None, onex=None, **settings):
    s = UserSettings(mode="crypto", auto=True, auto_confirm=False, auto_stake=5, auto_daily=25, auto_open=25,
                     auto_min=1.0, auto_hours=6, poly_funder="0x" + "1" * 40)
    for k, v in settings.items():
        setattr(s, k, v)
    store = SimpleNamespace(users={UID: s}, get=lambda uid: s, save=lambda: None)
    sent = []

    async def send(uid, text, spec=None):
        sent.append((text, spec))

    tmp = Path(tempfile.mkdtemp()) / "live.db"
    t = eng.AutoTrader(FakeService(arb), store, send, book=bk.Book(tmp), poly=poly or FakePoly(),
                       onexbit=onex or FakeOneXBit())
    t.book.set_balance(UID, "1xBit", 30.0)
    return t, s, sent


async def test_engine_success():
    a = make_arb()
    t, s, sent = make_trader(a)
    text, spec = await t.execute(UID, s, a)
    assert spec and spec[0] == "ticket", text
    r = t.book.run(spec[1])
    assert r["status"] == bk.OPEN and [b["bookie"] for b in r["bets"]] == ["1xBit", "Polymarket"]
    assert r["total"] <= 5 + 1e-9 and r["planned_profit"] > 0
    assert t.onexbit.calls and not t.onexbit.calls[0][3]  # really sent, 1xBit first
    token, amount, worst, _ = t.poly.calls[0]
    x = r["bets"][0]
    assert amount / worst >= r["total"] - 1e-6  # Polymarket's worst price still covers the total
    bal = t.book.balance(UID, "1xBit")
    assert abs(bal.now - (30 - x["stake"])) < 1e-9
    assert abs(t.book.exposure(UID) - r["total"]) < 1e-9
    # the same arb isn't played twice while its ticket is open
    assert t.candidates(s) == []


async def test_engine_1xbit_refused():
    a = make_arb()
    t, s, _ = make_trader(a, onex=FakeOneXBit("Coefficient changed"))
    text, spec = await t.execute(UID, s, a, manual=True)
    assert "nije primio" in text and spec is None
    assert not t.poly.calls  # Polymarket never touched
    assert t.book.exposure(UID) == 0 and t.book.balance(UID, "1xBit").now == 30
    assert t.book.runs(UID)[0]["status"] == bk.FAILED


async def test_engine_poly_fails_then_cover():
    a = make_arb()
    fail = "order couldn't be fully filled"
    t, s, _ = make_trader(a, poly=FakePoly(fills=[fail] * 4))
    eng.RETRY_WAIT = 0
    text, spec = await t.execute(UID, s, a)
    assert spec == ("exposed", spec[1]) and "NIJE" in text
    assert len(t.poly.calls) == eng.RETRIES + 1  # break-even tries + one at a small loss
    assert t.poly.calls[-1][2] > t.poly.calls[0][2]  # the cover allows a worse price
    r = t.book.run(spec[1])
    assert r["status"] == bk.EXPOSED and r["planned_profit"] < 0
    assert "potvrđen" in t.not_placed(UID, r["id"]) and t.book.run(r["id"])["status"] == bk.EXPOSED  # 1xBit is in
    text, spec2 = await t.cover(UID, r["id"])
    assert spec2 == ("ticket", r["id"]), text
    r = t.book.run(r["id"])
    assert r["status"] == bk.OPEN and all(b["status"] == bk.PLACED for b in r["bets"])


async def test_engine_cover_at_small_loss():
    a = make_arb()
    t, s, _ = make_trader(a, poly=FakePoly(fills=["no"] * eng.RETRIES + ["ok"]))
    text, spec = await t.execute(UID, s, a)
    r = t.book.run(spec[1])
    assert r["status"] == bk.OPEN and "gubit" in r["note"]


async def test_engine_unknown_1xbit():
    a = make_arb()
    t, s, _ = make_trader(a, onex=FakeOneXBit("unknown"))
    text, spec = await t.execute(UID, s, a)
    assert spec[0] == "unconfirmed" and not t.poly.calls
    msg = t.not_placed(UID, spec[1])
    assert "nije prošao" in msg and t.book.run(spec[1])["status"] == bk.FAILED
    assert t.book.exposure(UID) == 0 and t.book.balance(UID, "1xBit").now == 30


async def test_engine_guards():
    a = make_arb()
    t, s, _ = make_trader(a, poly=FakePoly(blocked=True))
    text, spec = await t.execute(UID, s, a, manual=True)
    assert "ne dozvoljava" in text and not t.onexbit.calls
    t, s, _ = make_trader(a)
    t.halted = True
    text, _ = await t.execute(UID, s, a, manual=True)
    assert "zaustavljeno" in text and not t.onexbit.calls
    t, s, _ = make_trader(a, auto_daily=3)
    t.book.add_bet(t.book.new_run(UID, "k", "n", "m", "tennis", a.event.start.isoformat()), UID, "1xBit", "12", "1",
                   "x", 2.0, 2.0, "id", bk.PLACED)
    text, _ = await t.execute(UID, s, a, manual=True)
    assert "dnevni limit" in text and not t.onexbit.calls
    t, s, _ = make_trader(make_arb(hours=10))  # starts later than the 6 h rule
    assert t.candidates(s) == []
    t, s, _ = make_trader(make_arb(hours=0.01))  # starts in a minute
    assert t.candidates(s) == []
    small = make_arb(x_odds=(1.45, 3.0), p_odds=(1.3, 4.0))  # the Polymarket side gets ~27 %
    t, s, _ = make_trader(small, auto_stake=3)
    text, _ = await t.execute(UID, s, small, manual=True)
    assert "prima od 1 $" in text and not t.onexbit.calls, text  # never 1xBit alone
    t, s, _ = make_trader(a)
    t.book.set_balance(UID, "1xBit", 0.5)  # no money on 1xBit
    text, _ = await t.execute(UID, s, a, manual=True)
    assert "Nema dovoljno novca" in text and not t.onexbit.calls


async def test_engine_dry():
    a = make_arb()
    t, s, _ = make_trader(a, auto=False)
    text, spec = await t.execute(UID, s, a, dry=True)
    assert "Proba" in text and spec is None
    assert t.onexbit.calls[0][3] and t.poly.calls[0][3]  # both only built, not sent
    assert t.book.exposure(UID) == 0 and t.book.runs(UID) == []  # not in the real book
    assert t.book.balance(UID, "1xBit").now == 30


async def test_engine_settle():
    a = make_arb()
    t, s, sent = make_trader(a)
    _, spec = await t.execute(UID, s, a)
    run = spec[1]
    with t.book._con() as con:  # the match was 3 h ago
        con.execute("UPDATE runs SET start = ? WHERE id = ?",
                    ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), run))
    eng.resolution = AsyncMock(return_value=None)  # not resolved yet
    await t.settle(UID)
    assert t.book.run(run)["status"] == bk.OPEN
    eng.resolution = AsyncMock(return_value=False)  # the Polymarket side lost -> 1xBit won
    await t.settle(UID)
    r = t.book.run(run)
    assert r["status"] == bk.SETTLED and r["winner"] == "1xBit" and r["profit"] > 0
    x = r["bets"][0]
    assert abs(t.book.balance(UID, "1xBit").now - (30 - x["stake"] + x["payout"])) < 1e-9
    assert t.book.exposure(UID) == 0
    assert "Meč završen" in sent[-1][0]
    day = t.book.day(UID, 0)
    assert day["settled_n"] == 1 and day["settled_profit"] > 0


async def test_engine_confirm_and_round():
    a = make_arb()
    t, s, sent = make_trader(a, auto_confirm=True)
    t.on_scan(lambda uid: True)
    await t._task
    assert sent and sent[-1][1][0] == "confirm" and not t.onexbit.calls
    token = sent[-1][1][1]
    text, spec = await t.confirm(UID, token)
    assert spec[0] == "ticket", text
    text, _ = await t.confirm(UID, token)  # a second click does nothing
    assert "ne važi" in text
    # without asking: the round bets on its own
    t, s, sent = make_trader(make_arb())
    t.on_scan(lambda uid: True)
    await t._task
    assert sent and sent[-1][1][0] == "ticket"


# ---------------------------------------------------------------- 🤖 Auto buttons

def fake_callback(data):
    c = MagicMock()
    c.data = data
    c.from_user.id = UID
    c.answer = AsyncMock()
    c.message.answer = AsyncMock()
    c.message.edit_text = AsyncMock()
    c.message.html_text = ""
    return c


def fake_message(text):
    m = MagicMock()
    m.text = text
    m.from_user.id = UID
    sent = MagicMock()
    sent.edit_text = AsyncMock()
    m.answer = AsyncMock(return_value=sent)
    m.delete = AsyncMock()
    m.sent = sent
    return m


def last(mock):
    args, kwargs = mock.await_args_list[-1]
    return str(args[0] if args else kwargs.get("text", ""))


async def test_ui():
    from arb.tg import auto as ui
    from arb.tg import handlers as h
    from arb.tg import keyboards as kb

    a = make_arb()
    t, s, _ = make_trader(a, auto=False)
    store = t.store
    service = t.service
    h.ALLOWED_USERS = set()
    assert any(b.text == kb.BTN_AUTO for row in kb.main_menu(s).keyboard for b in row)
    assert any(b.callback_data == "au:back" for row in kb.bookies_kb(s).inline_keyboard for b in row)

    m = fake_message("/bot")
    await ui.show_auto(m, service, store, t)
    out = last(m.answer)
    assert "1xBit + Polymarket" in out and "isključeno" in out and "Trenutno prolazi: <b>1</b>" in out, out

    c = fake_callback("au:toggle")
    await ui.cb_auto(c, service, store, t)
    assert s.auto and "uključeno" in last(c.message.edit_text)

    # 1xBit balance typed in
    await ui.cb_auto(fake_callback("au:1xbal"), service, store, t)
    m = fake_message("27,5")
    await ui.typed_auto(m, service, store, t)
    assert t.book.balance(UID, "1xBit").now == 27.5

    # rules
    await ui.cb_auto(fake_callback("au:st:3"), service, store, t)
    assert s.auto_stake == 3
    await ui.cb_auto(fake_callback("au:cmin"), service, store, t)
    await ui.typed_auto(fake_message("1.2"), service, store, t)
    assert s.auto_min == 1.2

    # Polymarket connect: type -> address -> key (deleted from the chat)
    t.poly = pm.PolyExecutor(client_factory=lambda key, funder, sig: SimpleNamespace(
        get_balance_allowance=lambda p: {"balance": "12340000"}))
    await ui.cb_auto(fake_callback("au:pmsig:1"), service, store, t)
    await ui.typed_auto(fake_message("0x" + "2" * 40), service, store, t)
    m = fake_message("ab" * 32)
    await ui.typed_auto(m, service, store, t)
    m.delete.assert_awaited()
    assert "povezano, balans 12.34 $" in last(m.sent.edit_text), last(m.sent.edit_text)
    assert s.poly_funder == "0x" + "2" * 40 and vault.get(pm.KEY_NAME) == "0x" + "ab" * 32

    # dry run, tickets, report, stop
    t.poly = FakePoly()
    c = fake_callback("au:dry")
    await ui.cb_auto(c, service, store, t)
    assert "Proba" in last(c.message.answer), last(c.message.answer)
    await t.execute(UID, s, a)
    c = fake_callback("au:tickets")
    await ui.cb_auto(c, service, store, t)
    assert "#" in last(c.message.edit_text)
    c = fake_callback("au:report")
    await ui.cb_auto(c, service, store, t)
    assert "uloženo danas" in last(c.message.answer)
    c = fake_callback("au:stop")
    await ui.cb_auto(c, service, store, t)
    assert not s.auto and t.halted
    run = t.book.runs(UID)[0]
    c = fake_callback(f"au:win:{run['id']}:{run['bets'][1]['id']}")
    await ui.cb_auto(c, service, store, t)
    assert t.book.run(run["id"])["winner"] == "Polymarket"
    assert ui.markup_for(("confirm", "ab"), t).inline_keyboard[0][0].callback_data == "au:go:ab"


# ---------------------------------------------------------------- runner

def main() -> None:
    bad = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            asyncio.run(fn()) if inspect.iscoroutinefunction(fn) else fn()
            print(f"✅ {name}")
        except Exception as e:
            bad += 1
            import traceback

            traceback.print_exc()
            print(f"❌ {name}: {e}")
    print("\n" + ("✅ Sve prošlo." if not bad else f"❌ Palo: {bad}"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
