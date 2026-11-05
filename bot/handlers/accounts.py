from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db, providers
from ..keyboards import PROVIDERS, back, kb

router = Router()


class AddAccount(StatesGroup):
    username = State()
    password = State()
    token = State()


def make_client(acc: db.Account):
    return providers.make_client(acc.provider, acc.username, acc.password, acc.project_name)


async def accounts_view(user_id: int, provider: str | None = None, select_prefix: str | None = None):
    """Account list. provider=None → all providers. select_prefix → buttons pick an account (buy flow)."""
    shown = [provider] if provider else list(PROVIDERS)
    rows, total = [], 0
    for prov in shown:
        accounts = await db.list_accounts(user_id, prov)
        total += len(accounts)
        for a in accounts:
            label = f"🔑 {a.username}" if provider else f"🔑 {PROVIDERS[prov]} · {a.username}"
            if select_prefix:
                rows.append([Btn(text=label, callback_data=f"{select_prefix}{a.id}")])
            else:
                rows.append([Btn(text=label, callback_data="noop"), Btn(text="🗑", callback_data=f"acc:del:{a.id}")])
    for prov in shown:
        rows.append([Btn(text=f"➕ Подключить {PROVIDERS[prov]}", callback_data=f"acc:add:{prov}")])
    rows.append(back("buy" if select_prefix else "menu"))
    title = PROVIDERS[provider] if provider else "провайдеров"
    text = f"🔑 <b>Аккаунты {title}</b>\n\n"
    text += "👇 Выберите аккаунт для покупки" if select_prefix and total else (
        f"✅ Подключено: <b>{total}</b>" if total else "📭 Пока нет подключённых аккаунтов.\n👇 Подключите аккаунт")
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
    await cb.answer("🗑 Аккаунт удалён")


CANCEL = kb(back("accounts", "❌ Отмена"))

TOKEN_HELP = {
    "selectel": ("1️⃣ Откройте vds.selectel.ru → <b>Настройки → API</b>.\n"
                 "2️⃣ Создайте токен с правом на <b>запись</b>.\n"
                 "3️⃣ Отправьте токен сюда."),
    "timeweb": ("1️⃣ Откройте timeweb.cloud → <b>API и Terraform</b>.\n"
                "2️⃣ Создайте токен (полный доступ, без ограничения по IP или с IP вашего сервера с ботом).\n"
                "3️⃣ Отправьте токен сюда.\n\n"
                "⚠️ Выключите <b>«Подтверждение удаления сервисов»</b> в настройках аккаунта — иначе Timeweb "
                "просит код из Telegram при каждом удалении, и бот не сможет удалять серверы с чужим IP."),
    "aeza": ("1️⃣ Откройте my.aeza.net → <b>Настройки → API</b>.\n"
             "2️⃣ Создайте API-ключ.\n"
             "3️⃣ Отправьте ключ сюда.\n\n"
             "💡 Серверы заказываются с почасовой оплатой с баланса аккаунта."),
}


@router.callback_query(F.data.startswith("acc:add:"))
async def add_start(cb: CallbackQuery, state: FSMContext):
    provider = cb.data.split(":")[2]
    await state.update_data(provider=provider)
    if provider in TOKEN_HELP:
        await state.set_state(AddAccount.token)
        text = (f"➕ <b>Подключение {PROVIDERS[provider]}</b>\n\n" + TOKEN_HELP[provider]
                + "\n\n🔒 Сообщение с ключом бот сразу удалит.")
    else:
        await state.set_state(AddAccount.username)
        text = ("➕ <b>Подключение HostVDS</b>\n\n"
                "📄 Нужны данные OpenStack API из панели HostVDS (файл <code>openrc.sh</code>).\n\n"
                "1️⃣ Отправьте <b>логин</b> (<code>OS_USERNAME</code>, обычно вида "
                "<code>hostvds-xxxxxxxx-...</code>).")
    await cb.message.edit_text(text, reply_markup=CANCEL)
    await cb.answer()


@router.message(AddAccount.username)
async def add_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await state.set_state(AddAccount.password)
    await message.answer("2️⃣ Отправьте <b>пароль</b> (<code>OS_PASSWORD</code>).\n\n"
                         "🔒 Сообщение с паролем бот сразу удалит.", reply_markup=CANCEL)


@router.message(AddAccount.password)
async def add_password(message: Message, state: FSMContext):
    data = await state.get_data()
    await _save(message, state, data["username"], message.text.strip(), data["username"])


@router.message(AddAccount.token)
async def add_token(message: Message, state: FSMContext):
    await _save(message, state, None, message.text.strip(), "")


async def _save(message: Message, state: FSMContext, username: str | None, secret: str, project: str):
    try:
        await message.delete()
    except Exception:
        pass
    provider = (await state.get_data())["provider"]
    wait = await message.answer("⏳ Проверяю доступ…")
    try:
        async with providers.make_client(provider, username or "", secret, project) as client:
            info = await client.check()
    except Exception as e:
        await state.clear()
        await wait.edit_text(f"❌ <b>Не удалось войти</b>\n\n<code>{hd.quote(str(e)[:500])}</code>\n\n"
                             "🔁 Проверьте данные и попробуйте ещё раз.",
                             reply_markup=kb([Btn(text="🔁 Заново", callback_data=f"acc:add:{provider}")],
                                             back("accounts")))
        return
    if username is None:  # token-based: name the account by the info from the API
        username = info.split(",")[0][:40] or f"…{secret[-6:]}"
    await db.add_account(message.from_user.id, provider, username, secret, project)
    await state.clear()
    await wait.edit_text(
        f"✅ <b>Аккаунт подключён!</b>\n\n☁️ {PROVIDERS[provider]}\n👤 <code>{hd.quote(username)}</code>\n"
        f"ℹ️ {hd.quote(info)}",
        reply_markup=kb([Btn(text="🛒 Купить сервер", callback_data="buy")], back("menu", "🏠 Главное меню")),
    )


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()
