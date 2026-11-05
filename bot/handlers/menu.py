from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..config import config
from ..keyboards import back, kb, main_menu

router = Router()


def _admin(user_id: int) -> bool:
    return user_id in config.admin_ids


def welcome(name: str) -> str:
    return (
        f"👋 Приветствую, <b><i>{hd.quote(name)}</i></b>\n\n"
        "VPS Buyer — покупка облачных серверов с нужным IP в одном месте.\n\n"
        "🟢 Подключи аккаунт провайдера\n"
        "🟢 Выбери локацию и IP-подсеть\n"
        "🟢 Бот докупает серверы, пока не выпадет нужный IP\n\n"
        "Выберите нужный раздел ниже."
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(welcome(message.from_user.full_name), reply_markup=main_menu(_admin(message.from_user.id)))


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(welcome(cb.from_user.full_name), reply_markup=main_menu(_admin(cb.from_user.id)))
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    uid = cb.from_user.id
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Подключено аккаунтов: <b>{await db.count_accounts(uid)}</b>\n"
        f"Куплено серверов: <b>{await db.count_servers(uid)}</b>"
    )
    await cb.message.edit_text(text, reply_markup=kb(back()))
    await cb.answer()


@router.callback_query(F.data == "info")
async def info(cb: CallbackQuery):
    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        "1. «🔑 Аккаунты» → подключите аккаунт HostVDS (логин и пароль OpenStack) или Selectel VDS (API-токен).\n"
        "2. «🛒 Купить сервер» → провайдер → аккаунт → локация.\n"
        "3. Бот покажет доступные IP-подсети (например 94.x, 123.x, 207.x) — выберите нужную.\n"
        "4. Укажите количество серверов, тариф и образ.\n"
        "5. Бот создаёт серверы. Если IP не из выбранной подсети — сервер удаляется "
        "и создаётся новый, пока не наберётся нужное количество.\n\n"
        "⚠️ Каждое создание сервера может списывать деньги (почасовая оплата). "
        "Есть лимит попыток — см. MAX_ATTEMPTS."
    )
    await cb.message.edit_text(text, reply_markup=kb(back()))
    await cb.answer()
