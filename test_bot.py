"""End-to-end check of the bot's handlers without Telegram:  python test_bot.py [rs|crypto|both]

Runs a real scan, then drives the same handler functions the buttons use (list,
clicking an arb number, refresh, custom stake, rs <-> crypto switch) with fake
Telegram objects, and reports what the user would see and how long each step took.
Run it before restarting the bot after code changes."""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from arb.tg import handlers as h
from arb.tg.service import ArbService, arb_key, cb_key
from arb.tg.storage import Storage, UserSettings

UID = 1
MAX_CLICK_SECONDS = 15
problems: list[str] = []


def fake_message(text: str = "") -> MagicMock:
    sent = MagicMock()
    sent.edit_text = AsyncMock()
    sent.delete = AsyncMock()
    sent.chat.id, sent.message_id = UID, 100
    m = MagicMock()
    m.text = text
    m.from_user.id = UID
    m.chat.id = UID
    m.answer = AsyncMock(return_value=sent)
    m.sent = sent
    return m


def fake_callback(data: str) -> MagicMock:
    c = MagicMock()
    c.data = data
    c.from_user.id = UID
    c.answer = AsyncMock()
    c.message = fake_message()
    c.message.html_text = ""
    c.message.chat.id, c.message.message_id = UID, 100
    c.message.edit_text = AsyncMock()
    c.message.edit_reply_markup = AsyncMock()
    return c


def last_text(mock: AsyncMock) -> str:
    if not mock.await_args_list:
        return ""
    args, kwargs = mock.await_args_list[-1]
    return str(args[0] if args else kwargs.get("text", ""))


async def timed(label: str, coro, limit: float | None = None):
    t = time.perf_counter()
    res = await coro
    dt = time.perf_counter() - t
    flag = ""
    if limit and dt > limit:
        flag = f"  ❌ sporo (> {limit}s)"
        problems.append(f"{label}: {dt:.1f}s")
    print(f"  {label}: {dt:.1f}s{flag}")
    return res


async def check_mode(service: ArbService, store: Storage, mode: str) -> None:
    s = store.get(UID)
    if s.mode != mode:
        s.switch_mode()
    print(f"\n=== režim {mode} ({', '.join(s.bookies)}) ===")
    await timed("scan", service.fresh(s))
    arbs = service.arbs_for(s)
    print(f"  arbitraža: {len(arbs)}")

    # 📋 Lista arbitraža
    m = fake_message("/arbitraze")
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    await timed("lista", h.show_list(m, bot, service, store), 3)
    print("   ", last_text(m.answer).splitlines()[0] if m.answer.await_args_list else "(ništa)")
    # 🎯 Srednjice
    m = fake_message(kb_btn := "🎯 Srednjice")
    await timed("srednjice", h.show_middles(m, service, store), 5)
    out = last_text(m.answer)
    print("   ", out.splitlines()[0] if out else "(ništa)")
    c = fake_callback("md:page:0")
    c.message.edit_text = AsyncMock()
    await timed("srednjice strana", h.cb_middles(c, service, store), 5)

    if not arbs:
        return

    # click the numbers of the top 3 arbs
    for arb in arbs[:3]:
        key = arb_key(arb)
        c = fake_callback(f"ad:{cb_key(key)}")
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        await timed(f"klik {arb.event.name[:40]}", h.cb_arb_detail(c, bot, service, store), MAX_CLICK_SECONDS)
        placeholder = last_text(c.message.answer)
        final = last_text(c.message.sent.edit_text)
        if "više ne postoji" in final and any(arb_key(a) == key for a in service.arbs_for(s)):
            problems.append(f"klik {key}: nestala arbitraža je i dalje u listi")
        if "Proveravam" not in placeholder:
            problems.append(f"klik: nema brze poruke ({placeholder[:60]})")
        if not final:
            problems.append(f"klik {key}: poruka nije popunjena")
        print("    ->", final.splitlines()[0][:90] if final else "(prazno)")
        for line in final.splitlines():
            if "kvote od pre" in line or "Ukupno" in line:
                print("      ", line.strip()[:100])

    # 🔄 Osveži + custom stake on the first arb
    key = arb_key(arbs[0])
    c = fake_callback(f"rf:{cb_key(key)}:{s.budget}")
    c.message.edit_text = AsyncMock()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    await timed("proveri kvote sad", h.cb_arb(c, bot, service, store), MAX_CLICK_SECONDS)
    print("    ->", [l.strip()[:110] for l in last_text(c.message.edit_text).splitlines() if "kvota" in l][:3])
    amount = "10.5" if mode == "crypto" else "37.5k"
    h.awaiting[UID] = (UID, 100, key)
    m = fake_message(amount)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    await timed(f"upisan ulog {amount}", h.typed_amount(m, bot, service, store), 5)
    out = last_text(bot.edit_message_text) or last_text(m.answer)
    print("    ->", [l.strip() for l in out.splitlines() if "uloži" in l][:3])


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "crypto"
    modes = ["rs", "crypto"] if which == "both" else [which]
    store = Storage.__new__(Storage)
    store.users = {UID: UserSettings()}
    store.save = lambda: None
    h.ALLOWED_USERS = set()  # type: ignore[attr-defined]
    service = ArbService(20)
    service.regions = lambda: {u.mode for u in store.users.values()}
    try:
        for mode in modes:
            await check_mode(service, store, mode)
        # the rs <-> crypto switch button
        s = store.get(UID)
        c = fake_callback("set:mode:")
        await timed("prebacivanje režima", h.cb_settings(c, service, store), 120)
        print("    ->", [last_text(c.message.answer)[:80]])
    finally:
        await service.close()
    print("\n" + ("✅ Sve radi." if not problems else "❌ Problemi:\n  - " + "\n  - ".join(problems)))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
