import json

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db, providers
from ..keyboards import PROVIDERS, back, kb
from ..providers import PROVIDER_NAMES

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
            label = f"🔑 {a.username}" if provider else f"🔑 {PROVIDER_NAMES[prov]} · {a.username}"
            if select_prefix:
                rows.append([Btn(text=label, callback_data=f"{select_prefix}{a.id}")])
            else:
                rows.append([Btn(text=label, callback_data="noop"), Btn(text="🗑", callback_data=f"acc:del:{a.id}")])
    for prov in shown:
        rows.append([Btn(text=f"➕ Подключить {PROVIDER_NAMES[prov]}", callback_data=f"acc:add:{prov}")])
    rows.append(back("buy" if select_prefix else "menu"))
    title = PROVIDER_NAMES[provider] if provider else "провайдеров"
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
    "serverspace": ("1️⃣ Откройте my.serverspace.ru → <b>API</b>.\n"
                    "2️⃣ Создайте API-ключ с правом <b>«VstackCloud: Edit»</b> (создание и удаление серверов).\n"
                    "3️⃣ Отправьте ключ сюда.\n\n"
                    "💡 Бот узнаёт целые подсети по маске IP — после первой покупки или разведки "
                    "в выборе IP появятся все блоки подсети."),
    "digitalocean": ("1️⃣ Откройте cloud.digitalocean.com → <b>API → Tokens</b>.\n"
                     "2️⃣ Создайте токен с правами <b>Read и Write</b>.\n"
                     "3️⃣ Отправьте токен сюда.\n\n"
                     "🌐 Бот покажет все подсети выбранного дата-центра — вы сами отметите нужные."),
    "hetzner": ("1️⃣ Откройте console.hetzner.cloud → проект → <b>Security → API tokens</b>.\n"
                "2️⃣ Создайте токен с правами <b>Read & Write</b>.\n"
                "3️⃣ Отправьте токен сюда."),
    "vultr": ("1️⃣ Откройте my.vultr.com → <b>Account → API</b>.\n"
              "2️⃣ Включите API и скопируйте ключ. В <b>Access Control</b> разрешите IP сервера с ботом "
              "(или «Allow All IPv4»).\n"
              "3️⃣ Отправьте ключ сюда.\n\n"
              "💡 Бот сначала берёт только IP (резервные, ~$0.004/час) и создаёт сервер лишь на IP из ваших "
              "подсетей — лимит серверов аккаунта почти не тратится."),
    "linode": ("1️⃣ Откройте cloud.linode.com → профиль → <b>API Tokens</b>.\n"
               "2️⃣ Создайте токен: <b>Linodes — Read/Write</b>, <b>Account — Read Only</b>.\n"
               "3️⃣ Отправьте токен сюда."),
    "civo": ("1️⃣ Откройте dashboard.civo.com → <b>Settings → Profile → Security</b>.\n"
             "2️⃣ Скопируйте API key.\n"
             "3️⃣ Отправьте ключ сюда."),
    "gcp": ("1️⃣ Откройте console.cloud.google.com → создайте <b>проект</b> и привяжите к нему "
            "<b>платёжный аккаунт</b> (Billing).\n"
            "2️⃣ <b>APIs & Services → Library</b> → включите <b>Compute Engine API</b>.\n"
            "3️⃣ <b>IAM & Admin → Service Accounts → Create</b>, роль <b>Compute Admin</b>.\n"
            "4️⃣ У сервисного аккаунта: <b>Keys → Add key → Create new key → JSON</b> — скачается файл.\n"
            "5️⃣ Отправьте этот <b>.json файл</b> сюда (как документ).\n\n"
            "💡 Бот берёт сначала только IP (статический), сервер создаёт лишь на IP из нужных подсетей."),
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
        text = (f"➕ <b>Подключение {PROVIDER_NAMES[provider]}</b>\n\n" + TOKEN_HELP[provider]
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
async def add_token(message: Message, state: FSMContext, bot: Bot):
    if message.document:  # Google Cloud: the service account key comes as a .json file
        if message.document.file_size and message.document.file_size > 100_000:
            return await message.answer("⚠️ Файл слишком большой — нужен JSON-ключ сервисного аккаунта.")
        buf = await bot.download(message.document)
        secret = buf.read().decode("utf-8", "replace")
        try:
            secret = json.dumps(json.loads(secret))  # compact, one line
        except ValueError:
            pass
    elif message.text:
        secret = message.text.strip()
    else:
        return await message.answer("⚠️ Отправьте ключ текстом" + (" или файлом" if
                                    (await state.get_data()).get("provider") == "gcp" else "") + ".")
    await _save(message, state, None, secret, "")


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
        f"✅ <b>Аккаунт подключён!</b>\n\n☁️ {PROVIDER_NAMES[provider]}\n👤 <code>{hd.quote(username)}</code>\n"
        f"ℹ️ {hd.quote(info)}",
        reply_markup=kb([Btn(text="🛒 Купить сервер", callback_data="buy")], back("menu", "🏠 Главное меню")),
    )


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()
