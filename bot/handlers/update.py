from aiogram import Dispatcher, F, Router
from aiogram.types import CallbackQuery, Update
from aiogram.utils.text_decorations import html_decoration as hd

from .. import updater
from ..config import config
from ..keyboards import back, kb
import asyncio

from aiogram.types import InlineKeyboardButton as Btn

from .buy import running_jobs

router = Router()

# Set when the bot must restart after polling stops: last handled update id.
RESTART: dict[str, int] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


@router.callback_query(F.data.in_({"update", "update:force"}))
async def update_bot(cb: CallbackQuery, dispatcher: Dispatcher, event_update: Update):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов (ADMIN_IDS)", show_alert=True)
    jobs = running_jobs()
    if jobs and cb.data == "update":
        mine = cb.from_user.id in jobs
        await cb.answer()
        await cb.message.edit_text(
            "⏳ <b>Сейчас идут покупки: " + str(len(jobs)) + "</b>\n"
            + ("• в том числе ваша\n" if mine else "")
            + (f"• других пользователей: {len(jobs) - mine}\n" if len(jobs) - mine else "")
            + "\nПри обновлении бот перезапустится, и они прервутся. Купленные серверы останутся в «Мои серверы».",
            reply_markup=kb([Btn(text="⛔ Остановить и обновить", callback_data="update:force")],
                            back("menu", "⬅️ Подождать")))
        return
    if jobs:  # update:force — stop everything first (servers already bought are saved)
        for job in jobs.values():
            job.cancelled = True
        await cb.message.edit_text("⛔ Останавливаю покупки…")
        await asyncio.wait([j.task for j in jobs.values()], timeout=90)
    await cb.answer()
    await cb.message.edit_text("⏳ Проверяю обновления…")
    try:
        result = await updater.update()
    except Exception as e:
        await cb.message.edit_text(f"❌ <b>Не удалось обновить</b>\n\n<code>{hd.quote(str(e)[:800])}</code>",
                                   reply_markup=kb(back()))
        return
    if not result.updated:
        version = await updater.current_version()
        await cb.message.edit_text(f"✅ <b>Уже последняя версия</b>\n\n🏷 <code>{hd.quote(version)}</code>",
                                   reply_markup=kb(back()))
        return
    changes = "\n".join(f"• {hd.quote(c)}" for c in result.changes[:15])
    text = (f"✅ <b>Бот обновлён</b>\n🏷 <code>{result.old}</code> → <code>{result.new}</code>\n\n"
            f"📝 <b>Что нового:</b>\n{changes}")
    await cb.message.edit_text(text[:3900] + "\n\n♻️ Перезапускаюсь…")
    updater.save_notify(cb.from_user.id, text[:3900])
    RESTART["update_id"] = event_update.update_id
    try:
        await dispatcher.stop_polling()
    except RuntimeError:  # not running via polling
        RESTART.clear()
        await cb.message.answer("♻️ Перезапустите бота вручную: <code>systemctl restart vps-buyer</code>")

