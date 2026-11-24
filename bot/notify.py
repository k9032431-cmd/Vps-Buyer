"""Announcements to everyone who ever wrote to the bot (new working subnets for users)."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.utils.text_decorations import html_decoration as hd

from . import db
from .keyboards import kb

log = logging.getLogger(__name__)
SEND_GAP = 0.05  # Telegram allows ~30 messages a second to different chats
_TASKS: set[asyncio.Task] = set()


def subnets_text(provider_name: str, location: str) -> str:
    return ("🆕 <b>Найдены новые рабочие подсети!</b>\n\n"
            f"☁️ Провайдер: <b>{hd.quote(provider_name)}</b>\n"
            f"📍 Локация: <b>{hd.quote(location)}</b>\n\n"
            "🚀 Серверы с рабочими IP уже можно покупать 👇")


async def broadcast(bot: Bot, text: str, markup, skip: set[int] = frozenset()) -> tuple[int, int]:
    """Send to every known person; returns (delivered, failed). People who blocked the bot are skipped later."""
    sent = failed = 0
    for uid in await db.broadcast_recipients():
        if uid in skip:
            continue
        for attempt in range(3):
            try:
                await bot.send_message(uid, text, reply_markup=markup)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:  # blocked the bot / deleted account
                await db.mark_blocked(uid)
                failed += 1
                break
            except Exception as e:
                log.info("broadcast to %s failed: %s", uid, e)
                failed += 1
                break
        await asyncio.sleep(SEND_GAP)
    return sent, failed


def announce_subnets(bot: Bot, admin_chat: int, provider_name: str, location: str) -> asyncio.Task:
    """In the background: tell everyone about new working subnets, then report to the admin."""
    async def run():
        try:
            sent, failed = await broadcast(bot, subnets_text(provider_name, location),
                                           kb([Btn(text="🛒 Купить сервер", callback_data="buy")]),
                                           skip={admin_chat})
            await bot.send_message(admin_chat, f"📣 <b>Уведомление разослано</b>\n\n☁️ {hd.quote(provider_name)} · "
                                               f"📍 {hd.quote(location)}\n✅ Доставлено: <b>{sent}</b>"
                                               + (f"\n🚫 Не доставлено: {failed}" if failed else ""))
        except Exception:
            log.exception("subnet announcement failed")
    task = asyncio.create_task(run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
