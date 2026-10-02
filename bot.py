"""Telegram bot:  python bot.py"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from arb.config import ALLOWED_USERS, DATA_DIR, SCAN_INTERVAL, TELEGRAM_TOKEN
from arb.live.engine import AutoTrader
from arb.tg import auto
from arb.tg.formatting import TZ
from arb.tg.handlers import Notifier, allowed, denied, router
from arb.tg.service import ArbService
from arb.tg.storage import Storage

log = logging.getLogger("bot")


async def main() -> None:
    if not TELEGRAM_TOKEN:
        print("❌ Nema tokena. Upiši TELEGRAM_TOKEN u fajl .env (token dobijaš od @BotFather).")
        sys.exit(2)  # start.bat: config error, don't auto-restart
    if not ALLOWED_USERS:
        log.warning("ALLOWED_USERS je prazan - bota može da koristi bilo ko ko ga nađe!")

    bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    service = ArbService(SCAN_INTERVAL)
    store = Storage()
    notifier = Notifier(bot, service, store)
    trader_ref: list = []
    trader = AutoTrader(service, store, auto.make_sender(bot, trader_ref), tz=TZ)
    trader_ref.append(trader)
    notifier.trader = trader  # 🤖 real bets (1xBit + Polymarket) after every scan
    service.on_scan = notifier
    # scan only the bookies someone is using (Serbian / crypto)
    service.regions = lambda: {s.mode for uid, s in store.users.items() if allowed(uid)}

    dp = Dispatcher(service=service, store=store, trader=trader)
    dp.include_routers(auto.router, router, denied)  # 🤖 first: it takes the typed keys / amounts it asked for
    await bot.set_my_commands([
        BotCommand(command="arbitraze", description="Lista svih arbitraža (osvežava se sama)"),
        BotCommand(command="bot", description="Bot: test na papiru (rok, min %, max ulog)"),
        BotCommand(command="auto", description="Prave uplate 1xBit + Polymarket: nalozi, balans, tiketi, 🛑 Stop"),
        BotCommand(command="start", description="Glavni meni"),
    ])

    me = await bot.get_me()
    log.info("Bot @%s pokrenut, scan na svakih %ds", me.username, SCAN_INTERVAL)
    scan_task = asyncio.create_task(service.loop())
    try:
        await dp.start_polling(bot)
    finally:
        scan_task.cancel()
        await trader.close()
        await service.close()
        await bot.session.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    DATA_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_log = RotatingFileHandler(DATA_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file_log, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_log, console])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
