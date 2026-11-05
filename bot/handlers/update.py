from aiogram import Dispatcher, F, Router
from aiogram.types import CallbackQuery, Update
from aiogram.utils.text_decorations import html_decoration as hd

from .. import updater
from ..config import config
from ..keyboards import back, kb
from .buy import JOBS

router = Router()

# Set when the bot must restart after polling stops: last handled update id.
RESTART: dict[str, int] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


@router.callback_query(F.data == "update")
async def update_bot(cb: CallbackQuery, dispatcher: Dispatcher, event_update: Update):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов (ADMIN_IDS)", show_alert=True)
    if JOBS:
        return await cb.answer("⏳ Сейчас идёт покупка серверов — дождитесь окончания.", show_alert=True)
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

