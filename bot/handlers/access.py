"""Invite-only access: admins (ADMIN_IDS) decide who may use the bot.

Everyone else sees a short presentation of the bot, their Telegram ID and a button that opens a chat
with the admin (ADMIN_CONTACT). The admin grants access in "👥 Users → ➕ Add by ID"."""
import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message, Update, User
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..config import config
from ..keyboards import back, kb, main_menu

router = Router()
log = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def has_access(user_id: int) -> bool:
    if not config.admin_ids:  # no admins configured → open bot (warned about at start)
        return True
    if is_admin(user_id):
        return True
    user = await db.get_user(user_id)
    return bool(user and user.status == "approved")


# ---------- what people without access see ----------
def landing_text(user: User) -> str:
    return (
        f"👋 Привет, <b>{hd.quote(user.full_name)}</b>!\n\n"
        "🤖 <b>VPS Buyer</b> — бот для покупки VPS-серверов у разных провайдеров "
        "с рабочими открытыми подсетями.\n\n"
        "🎯 <b>Нужный IP:</b> выбираете подсети — бот сам подбирает сервер с подходящим адресом\n"
        "⚡ <b>Быстро:</b> подбор IP без лишних покупок, готовый сервер сразу в чат\n"
        "🔑 <b>Сразу в работу:</b> IP, пароль root и команда SSH в одном сообщении\n"
        "🗂 <b>Управление:</b> список серверов, удаление, восстановление доступа\n\n"
        "🔒 <b>Доступ только по приглашению.</b>\n"
        f"Для доступа напишите администратору @{config.admin_contact} и отправьте ему ваш ID:\n\n"
        f"🆔 <code>{user.id}</code>"
    )


def landing_kb():
    return kb([Btn(text="💬 Написать админу", url=f"https://t.me/{config.admin_contact}")])


def _user_line(user_id: int, username: str, full_name: str) -> str:
    name = hd.quote(full_name or str(user_id))
    line = f'<a href="tg://user?id={user_id}">{name}</a>'
    if username:
        line += f" · @{hd.quote(username)}"
    return line + f" · <code>{user_id}</code>"


async def _show_landing(event: Update, user: User) -> None:
    if event.callback_query:
        cq = event.callback_query
        await cq.answer("🔒 Нет доступа", show_alert=False)
        if cq.message:
            try:
                await cq.message.edit_text(landing_text(user), reply_markup=landing_kb())
            except Exception:
                await cq.message.answer(landing_text(user), reply_markup=landing_kb())
    elif event.message:
        await event.message.answer(landing_text(user), reply_markup=landing_kb())


class AccessMiddleware(BaseMiddleware):
    """Outer middleware: approved users and admins go on; everyone else gets the landing screen."""

    async def __call__(self, handler, event: Update, data):
        user: User | None = data.get("event_from_user")
        if not user:
            return None
        if await has_access(user.id):
            return await handler(event, data)
        try:
            await _show_landing(event, user)
        except Exception:
            log.exception("access gate failed")
        return None


# ---------- admin side ----------
async def _decide(cb: CallbackQuery, bot: Bot, user_id: int, approve: bool) -> None:
    target = await db.get_user(user_id)
    await db.set_user_status(user_id, "approved" if approve else "denied", cb.from_user.id,
                             target.username if target else "", target.full_name if target else "")
    who = _user_line(user_id, target.username, target.full_name) if target else f"<code>{user_id}</code>"
    verdict = "✅ <b>Доступ выдан</b>" if approve else "❌ <b>Запрос отклонён</b>"
    try:
        await cb.message.edit_text(f"{verdict}\n\n👤 {who}\n🛡 Решил: {hd.quote(cb.from_user.full_name)}")
    except Exception:
        pass
    try:
        if approve:
            await bot.send_message(user_id, "🎉 <b>Доступ открыт!</b>\n\nДобро пожаловать в VPS Buyer. "
                                            "Начните с «🔑 Аккаунты» — подключите аккаунт провайдера 👇",
                                   reply_markup=main_menu())
        else:
            await bot.send_message(user_id, "😔 <b>В доступе отказано</b>\n\n"
                                            f"Если это ошибка — напишите @{config.admin_contact}",
                                   reply_markup=kb([Btn(text="💬 Написать админу",
                                                        url=f"https://t.me/{config.admin_contact}")]))
    except Exception:
        log.exception("can't notify user %s", user_id)


@router.callback_query(F.data.regexp(r"^usr:(ok|no):\d+$"))
async def decide(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов", show_alert=True)
    _, action, uid = cb.data.split(":")
    await _decide(cb, bot, int(uid), action == "ok")
    await cb.answer("✅ Готово")


async def users_screen(user_id: int):
    pending = await db.list_users("pending")
    approved = await db.list_users("approved")
    text = "👥 <b>Пользователи</b>\n\n"
    rows = []
    if pending:
        text += f"🙋 <b>Ждут доступа: {len(pending)}</b>\n"
        for u in pending[:15]:
            text += f"• {_user_line(u.user_id, u.username, u.full_name)}\n"
            rows.append([Btn(text=f"✅ {u.full_name[:20] or u.user_id}", callback_data=f"usr:okl:{u.user_id}"),
                         Btn(text="❌", callback_data=f"usr:nol:{u.user_id}")])
        text += "\n"
    text += f"✅ <b>С доступом: {len(approved)}</b>\n"
    text += "\n".join(f"• {_user_line(u.user_id, u.username, u.full_name)}" for u in approved[:30]) or "—"
    for u in approved[:30]:
        rows.append([Btn(text=f"🚫 Забрать доступ · {u.full_name[:20] or u.user_id}",
                         callback_data=f"usr:rm:{u.user_id}")])
    text += f"\n\n🛡 Админы (из ADMIN_IDS): {', '.join(f'<code>{a}</code>' for a in sorted(config.admin_ids))}"
    text += "\n\n💡 Человек без доступа видит в боте свой ID — попросите его прислать и нажмите «➕ Добавить по ID»."
    rows.append([Btn(text="➕ Добавить по ID", callback_data="usr:add")])
    rows.append(back("menu", "🏠 Главное меню"))
    return text[:4000], kb(*rows)


@router.callback_query(F.data == "users")
async def users(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов", show_alert=True)
    await state.clear()
    text, markup = await users_screen(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^usr:(okl|nol|rm):\d+$"))
async def users_action(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов", show_alert=True)
    _, action, uid = cb.data.split(":")
    uid = int(uid)
    target = await db.get_user(uid)
    if action == "rm":
        await db.set_user_status(uid, "denied", cb.from_user.id)
        try:
            await bot.send_message(uid, f"🚫 <b>Доступ к боту закрыт</b>\n\nВопросы — @{config.admin_contact}")
        except Exception:
            pass
        await cb.answer("🚫 Доступ забран")
    else:
        approve = action == "okl"
        await db.set_user_status(uid, "approved" if approve else "denied", cb.from_user.id,
                                 target.username if target else "", target.full_name if target else "")
        try:
            if approve:
                await bot.send_message(uid, "🎉 <b>Доступ открыт!</b>\n\nДобро пожаловать в VPS Buyer 👇",
                                       reply_markup=main_menu())
            else:
                await bot.send_message(uid, f"😔 <b>В доступе отказано</b>\n\nВопросы — @{config.admin_contact}")
        except Exception:
            pass
        await cb.answer("✅ Доступ выдан" if approve else "❌ Отклонено")
    text, markup = await users_screen(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)


class AddUser(StatesGroup):
    user_id = State()


@router.callback_query(F.data == "usr:add")
async def add_user(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🔒 Только для админов", show_alert=True)
    await state.set_state(AddUser.user_id)
    await cb.message.edit_text("➕ <b>Выдать доступ по ID</b>\n\nОтправьте Telegram ID пользователя "
                               "(узнать можно в @userinfobot).", reply_markup=kb(back("users", "❌ Отмена")))
    await cb.answer()


@router.message(AddUser.user_id)
async def add_user_id(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠️ Нужен числовой ID, например <code>123456789</code>")
        return
    uid = int(raw)
    await db.set_user_status(uid, "approved", message.from_user.id)
    await state.clear()
    try:
        await bot.send_message(uid, "🎉 <b>Доступ открыт!</b>\n\nДобро пожаловать в VPS Buyer 👇",
                               reply_markup=main_menu())
        note = "Пользователь получил уведомление."
    except Exception:
        note = "Уведомить не получилось — пользователь ещё не запускал бота. Доступ появится, когда он нажмёт /start."
    text, markup = await users_screen(message.from_user.id)
    await message.answer(f"✅ Доступ для <code>{uid}</code> выдан. {note}\n\n" + text, reply_markup=markup)
