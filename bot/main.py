import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import db, updater
from .config import config
from .handlers import access, accounts, buy, menu, servers, update

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.bot_token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    if not config.admin_ids:
        log.warning("ADMIN_IDS пуст — бот открыт для всех! Укажите свой Telegram ID, чтобы включить доступ по приглашению.")
    if not config.encryption_key:
        log.warning("ENCRYPTION_KEY не задан — пароли аккаунтов хранятся в БД открытым текстом.")
    await db.init_db()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(access.AccessMiddleware())
    dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)

    notify = updater.pop_notify()
    if notify:
        from .keyboards import main_menu
        try:
            await bot.send_message(notify["chat_id"], notify["text"] + f"\n\n🟢 <b>Бот снова в строю</b>\n🏷 "
                                   f"<code>{await updater.current_version()}</code>", reply_markup=main_menu(True))
        except Exception:
            log.exception("update notify failed")

    await dp.start_polling(bot)

    if "update_id" in update.RESTART:
        # Confirm the "update" button press to Telegram so it is not delivered again after restart.
        await bot.get_updates(offset=update.RESTART["update_id"] + 1, timeout=0, limit=1)
        await bot.session.close()
        log.info("Restarting after update…")
        updater.restart_process()


if __name__ == "__main__":
    asyncio.run(main())
