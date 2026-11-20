from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..config import config
from ..providers import PROVIDERS
from ..keyboards import back, kb, main_menu

router = Router()


def _admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def _menu(user_id: int):
    admin = _admin(user_id)
    return main_menu(admin, len(await db.list_users("pending")) if admin else 0)


def welcome(name: str) -> str:
    return (
        f"👋 Привет, <b>{hd.quote(name)}</b>!\n\n"
        "🤖 <b>VPS Buyer</b> покупает облачные серверы с нужным IP.\n\n"
        "🔑 Подключите аккаунт провайдера\n"
        "📍 Выберите провайдера и локацию\n"
        "🎯 Бот подбирает IP и присылает готовые серверы\n\n"
        "👇 Выберите раздел"
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(welcome(message.from_user.full_name), reply_markup=await _menu(message.from_user.id))


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(welcome(cb.from_user.full_name), reply_markup=await _menu(cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    uid = cb.from_user.id
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"🔑 Аккаунтов: <b>{await db.count_accounts(uid)}</b>\n"
        f"🖥 Серверов: <b>{await db.count_servers(uid)}</b>"
    )
    await cb.message.edit_text(text, reply_markup=kb(back("menu", "🏠 Главное меню")))
    await cb.answer()


@router.callback_query(F.data == "info")
async def info(cb: CallbackQuery):
    step3 = ("3️⃣ Отметьте нужные подсети, например <code>95.182.87.#</code>, или «🌐 Любой IP». "
             "«📌 Сохранить для пользователей» — из этих подсетей будут покупать все.\n\n"
             if _admin(cb.from_user.id) or not config.admin_ids else
             "3️⃣ Рабочие подсети уже подобраны админом — выбирать ничего не нужно.\n\n")
    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        "1️⃣ <b>🔑 Аккаунты</b> — подключите аккаунт провайдера (" + ", ".join(PROVIDERS.values()) + ").\n\n"
        "2️⃣ <b>🛒 Купить сервер</b> — провайдер → аккаунт → локация.\n\n"
        + step3 +
        "4️⃣ Укажите количество, тариф, Network plan и ОС.\n\n"
        "5️⃣ Бот подбирает IP, проверяет каждый подходящий сервер и присылает только рабочие — с паролем или "
        "вашим ключом. Остальные удаляются.\n\n"
        "☁️ <b>Мои серверы</b> — по провайдерам: пароли, удаление и «🛠 Восстановить доступ».\n\n"
        f"💸 Каждое создание сервера может стоить денег (почасовая оплата). "
        + (f"Лимит попыток: <b>{config.max_attempts}</b>." if config.max_attempts > 0
           else "Лимита попыток нет — покупку можно остановить кнопкой ⛔.")
    )
    await cb.message.edit_text(text, reply_markup=kb(back("menu", "🏠 Главное меню")))
    await cb.answer()
