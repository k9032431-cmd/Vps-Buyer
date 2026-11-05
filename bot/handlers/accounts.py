from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..config import config
from ..hostvds import HostVDSClient, HostVDSError
from ..keyboards import PROVIDERS, back, kb

router = Router()


class AddAccount(StatesGroup):
    username = State()
    password = State()


def make_client(acc: db.Account) -> HostVDSClient:
    return HostVDSClient(config.hostvds_auth_url, acc.username, acc.password, acc.project_name)


async def accounts_view(user_id: int, provider: str = "hostvds", select_prefix: str | None = None):
    """Account list. If select_prefix is set, buttons pick an account (used by the buy flow)."""
    accounts = await db.list_accounts(user_id, provider)
    rows = []
    for a in accounts:
        if select_prefix:
            rows.append([Btn(text=f"🔑 {a.username}", callback_data=f"{select_prefix}{a.id}")])
        else:
            rows.append([Btn(text=f"🔑 {a.username}", callback_data="noop"),
                         Btn(text="🗑", callback_data=f"acc:del:{a.id}")])
    rows.append([Btn(text="➕ Подключить аккаунт", callback_data=f"acc:add:{provider}")])
    rows.append(back("buy" if select_prefix else "menu"))
    text = f"🔑 <b>Аккаунты {PROVIDERS[provider]}</b>\n\n"
    text += "Выберите аккаунт:" if select_prefix and accounts else (
        f"Подключено: {len(accounts)}" if accounts else "Нет подключённых аккаунтов.")
    return text, kb(*rows)


@router.callback_query(F.data == "accounts")
async def accounts(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await accounts_view(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("acc:del:"))
async def delete(cb: CallbackQuery):
    await db.delete_account(cb.from_user.id, int(cb.data.split(":")[2]))
    text, markup = await accounts_view(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("acc:add:"))
async def add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddAccount.username)
    await state.update_data(provider=cb.data.split(":")[2])
    await cb.message.edit_text(
        "➕ <b>Подключение HostVDS</b>\n\n"
        "Нужны данные OpenStack API из панели HostVDS (файл <code>openrc.sh</code> / раздел API).\n\n"
        "Шаг 1/2. Отправьте <b>логин</b> (<code>OS_USERNAME</code>, обычно вида "
        "<code>hostvds-xxxxxxxx-...</code>).",
        reply_markup=kb(back("accounts", "❌ Отмена")),
    )
    await cb.answer()


@router.message(AddAccount.username)
async def add_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await state.set_state(AddAccount.password)
    await message.answer("Шаг 2/2. Отправьте <b>пароль</b> (<code>OS_PASSWORD</code>).\n"
                         "Сообщение с паролем будет удалено.", reply_markup=kb(back("accounts", "❌ Отмена")))


@router.message(AddAccount.password)
async def add_password(message: Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    username = data["username"]
    acc = db.Account(0, message.from_user.id, data["provider"], username, password, username)
    wait = await message.answer("⏳ Проверяю доступ…")
    try:
        async with make_client(acc) as client:
            await client.authenticate()
            regions = client.regions()
    except (HostVDSError, Exception) as e:
        await wait.edit_text(f"❌ Не удалось войти: {hd.quote(str(e))}\n\nПопробуйте ещё раз.",
                             reply_markup=kb([Btn(text="🔁 Заново", callback_data=f"acc:add:{data['provider']}")],
                                             back("accounts")))
        await state.clear()
        return
    await db.add_account(message.from_user.id, data["provider"], username, password, username)
    await state.clear()
    await wait.edit_text(
        f"✅ Аккаунт <code>{hd.quote(username)}</code> подключён.\n"
        f"Доступные локации: {', '.join(regions) or '—'}",
        reply_markup=kb([Btn(text="🛒 Купить сервер", callback_data="buy")], back("menu", "🏠 Главное меню")),
    )


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()
