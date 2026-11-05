import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import db
from .config import config
from .handlers import accounts, buy, menu

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Drops updates from users not in ADMIN_IDS (when ADMIN_IDS is set)."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if config.admin_ids and (not user or user.id not in config.admin_ids):
            return None
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.bot_token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    if not config.admin_ids:
        log.warning("ADMIN_IDS пуст — бот доступен всем пользователям Telegram!")
    if not config.encryption_key:
        log.warning("ENCRYPTION_KEY не задан — пароли аккаунтов хранятся в БД открытым текстом.")
    await db.init_db()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AccessMiddleware())
    dp.include_routers(menu.router, accounts.router, buy.router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
