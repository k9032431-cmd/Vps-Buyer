import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..keyboards import back, kb
from ..providers import PROVIDER_NAMES, Server
from .accounts import make_client

router = Router()
log = logging.getLogger(__name__)


async def _delete(user_id: int, row: db.ServerRow) -> str | None:
    """Deletes the server at the provider and from the list. Returns an error text or None."""
    acc = await db.get_account(user_id, row.account_id)
    if acc:
        try:
            async with make_client(acc) as client:
                await client.delete_server(row.region, row.server_id)  # 404 = already gone, that's fine
        except Exception as e:
            return str(e)
    # No account any more → nothing to call, just drop it from the list.
    await db.delete_server_row(user_id, row.id)
    return None


OTHER = "other"  # servers whose account was deleted


def _name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, "Без аккаунта")


async def _by_provider(user_id: int) -> dict[str, list[db.ServerRow]]:
    """The user's servers grouped by provider, providers in the bot's usual order."""
    rows = await db.list_servers(user_id, limit=1000)
    providers: dict[int, str] = {}
    for r in rows:
        if r.account_id not in providers:
            acc = await db.get_account(user_id, r.account_id)
            providers[r.account_id] = acc.provider if acc else OTHER
    groups: dict[str, list[db.ServerRow]] = {}
    for r in rows:
        groups.setdefault(providers[r.account_id], []).append(r)
    order = list(PROVIDER_NAMES) + [OTHER]
    return dict(sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))


def _line(r: db.ServerRow) -> str:
    return f"🖥 <code>{r.ip}</code>  ·  📍 {hd.quote(r.region)}"


LIVE_TIMEOUT = 25
BOT_NAMES = ("vps-", "ipscan-")  # names the bot gives its servers
# (user, account) → the account's servers as last loaded; buttons refer to them by index
LIVE: dict[tuple[int, int], list[Server]] = {}


async def _accounts(user_id: int) -> list[db.Account]:
    result = []
    for prov in PROVIDER_NAMES:
        result += await db.list_accounts(user_id, prov)
    return result


async def _live(user_id: int, acc: db.Account) -> list[Server] | None:
    """Every server in the provider account right now (None: the provider can't list or didn't answer)."""
    try:
        async with make_client(acc) as client:
            if not hasattr(client, "list_servers"):
                return None
            servers_ = await asyncio.wait_for(client.list_servers(), LIVE_TIMEOUT)
    except Exception as e:
        log.warning("list_servers failed for account %s: %s", acc.id, e)
        return None
    LIVE[(user_id, acc.id)] = servers_
    return servers_


def _status(s: Server) -> str:
    return "" if s.status == "ACTIVE" else f" · ⏳ {hd.quote(s.status.lower())}"


@router.callback_query(F.data == "servers")
async def servers(cb: CallbackQuery):
    uid = cb.from_user.id
    accounts = await _accounts(uid)
    groups = await _by_provider(uid)
    if not accounts and not groups:
        await cb.message.edit_text(
            "☁️ <b>Мои серверы</b>\n\n📭 Пока пусто.\nНажмите «🛒 Купить сервер» в главном меню.",
            reply_markup=kb([Btn(text="🛒 Купить сервер", callback_data="buy")], back("menu", "🏠 Главное меню")))
        return await cb.answer()
    await cb.answer()
    await cb.message.edit_text("⏳ Смотрю серверы в аккаунтах…")
    live = await asyncio.gather(*(_live(uid, a) for a in accounts))
    saved = await db.list_servers(uid, limit=1000)
    text, buttons, total = "☁️ <b>Мои серверы</b>\n", [], 0
    for prov in PROVIDER_NAMES:
        accs = [(a, l) for a, l in zip(accounts, live) if a.provider == prov]
        if not accs:
            continue
        text += f"\n<b>☁️ {_name(prov)}</b>\n"
        for a, l in accs:
            ours = sum(1 for r in saved if r.account_id == a.id)
            if l is not None:  # bot purchases still in the account
                ours = len({r.server_id for r in saved if r.account_id == a.id} & {x.id for x in l})
            if l is None:
                text += f"👤 {hd.quote(a.username)} — от бота: <b>{ours}</b> (список аккаунта недоступен)\n"
                count = ours
            else:
                text += (f"👤 {hd.quote(a.username)} — в аккаунте: <b>{len(l)}</b>"
                         + (f" · 🤖 от бота: {ours}" if ours else "") + "\n")
                count = len(l)
            total += count
            buttons.append([Btn(text=f"☁️ {_name(prov)} · {a.username}"[:50] + f" · {count}",
                                callback_data=f"srv:a:{a.id}")])
    if OTHER in groups:
        text += f"\n<b>🗂 Без аккаунта</b> · {len(groups[OTHER])}\n"
        buttons.append([Btn(text=f"🗂 Без аккаунта · {len(groups[OTHER])}", callback_data=f"srv:p:{OTHER}")])
    text += f"\n🖥 Всего серверов: <b>{total}</b>\n👇 Откройте аккаунт, чтобы увидеть серверы, пароли и удалить лишние."
    buttons.append([Btn(text="🔄 Обновить", callback_data="servers")])
    if saved:
        buttons.append([Btn(text="🗑 Удалить все от бота", callback_data="srv:delall")])
    await cb.message.edit_text(text[:4000], reply_markup=kb(*buttons, back("menu", "🏠 Главное меню")))


def _leftover(s: Server, ours: set[str]) -> bool:
    return s.id not in ours and s.name.startswith(BOT_NAMES)


async def _account_view(uid: int, acc: db.Account, reload: bool = True) -> tuple[str, object]:
    live = await _live(uid, acc) if reload or (uid, acc.id) not in LIVE else LIVE[(uid, acc.id)]
    rows = [r for r in await db.list_servers(uid, limit=1000) if r.account_id == acc.id]
    by_id = {r.server_id: r for r in rows}
    title = f"☁️ <b>{_name(acc.provider)}</b> · 👤 {hd.quote(acc.username)}\n"
    buttons = []
    if live is None:
        text = title + f"\n⚠️ Список серверов аккаунта сейчас недоступен — показаны купленные ботом: {len(rows)}\n\n"
        text += "\n".join(_line(r) for r in rows) or "📭 Нет серверов от бота."
        buttons = [[Btn(text=f"🖥 {r.ip}  ·  📍 {r.region}", callback_data=f"srv:{r.id}")] for r in rows]
    else:
        text = title + f"🖥 В аккаунте: <b>{len(live)}</b>\n\n"
        lines = []
        for i, s in enumerate(live):
            row = by_id.get(s.id)
            ip = s.ipv4[0] if s.ipv4 else "без IP"
            mark = "🤖" if row else "🧹" if _leftover(s, set(by_id)) else "🖥"
            lines.append(f"{mark} <code>{ip}</code>  ·  📍 {hd.quote(s.region or '?')}{_status(s)}")
            buttons.append([Btn(text=f"{mark} {ip}  ·  📍 {s.region}"[:60],
                                callback_data=f"srv:{row.id}" if row else f"srv:x:{acc.id}:{i}")])
        gone = [r for r in rows if r.server_id not in {s.id for s in live}]
        for r in gone:  # saved by the bot but not in the account any more (deleted in the panel)
            lines.append(f"👻 <code>{r.ip}</code> — уже нет в аккаунте")
            buttons.append([Btn(text=f"👻 {r.ip} — убрать из списка", callback_data=f"srv:yes:{r.id}")])
        text += "\n".join(lines[:60]) or "📭 Серверов нет."
        if len(lines) > 60:
            text += f"\n… и ещё {len(lines) - 60}"
        legend = []
        if any(by_id.get(s.id) for s in live):
            legend.append("🤖 — куплен ботом (есть пароль)")
        if any(_leftover(s, set(by_id)) for s in live):
            legend.append("🧹 — пробный сервер бота, который остался (можно удалить)")
        if any(not by_id.get(s.id) and not _leftover(s, set(by_id)) for s in live):
            legend.append("🖥 — создан не ботом")
        if legend:
            text += "\n\n" + "\n".join(legend)
        leftovers = [s for s in live if _leftover(s, set(by_id))]
        if leftovers:
            buttons.append([Btn(text=f"🧹 Удалить остатки бота · {len(leftovers)}", callback_data=f"srv:cl:{acc.id}")])
    buttons = buttons[:90]
    buttons.append([Btn(text="🔄 Обновить", callback_data=f"srv:a:{acc.id}")])
    return text[:4000], kb(*buttons, back("servers", "⬅️ Все аккаунты"))


@router.callback_query(F.data.regexp(r"^srv:a:\d+$"))
async def account_servers(cb: CallbackQuery):
    acc = await db.get_account(cb.from_user.id, int(cb.data.split(":")[2]))
    if not acc:
        return await cb.answer("Аккаунт не найден", show_alert=True)
    await cb.answer()
    await cb.message.edit_text("⏳ Загружаю серверы аккаунта…")
    text, markup = await _account_view(cb.from_user.id, acc)
    await cb.message.edit_text(text, reply_markup=markup)


def _foreign(uid: int, data: str) -> tuple[int, Server | None]:
    _, _, acc_id, idx = data.split(":")
    servers_ = LIVE.get((uid, int(acc_id))) or []
    return int(acc_id), servers_[int(idx)] if int(idx) < len(servers_) else None


@router.callback_query(F.data.regexp(r"^srv:x:\d+:\d+$"))
async def foreign_card(cb: CallbackQuery):
    acc_id, s = _foreign(cb.from_user.id, cb.data)
    if not s:
        return await cb.answer("🔄 Список устарел — обновите", show_alert=True)
    ip = s.ipv4[0] if s.ipv4 else "без IP"
    text = (f"🖥 <b>{hd.quote(s.name or s.id)}</b>\n\n🌐 IP: <code>{ip}</code>\n📍 Локация: {hd.quote(s.region or '?')}"
            f"\n⚙️ Статус: {hd.quote(s.status.lower())}\n\n"
            + ("🧹 <i>Пробный сервер бота, который не удалился. Его можно смело удалить.</i>"
               if s.name.startswith(BOT_NAMES) else
               "ℹ️ <i>Этот сервер создан не ботом — пароля от него у бота нет.</i>"))
    await cb.message.edit_text(text, reply_markup=kb(
        [Btn(text="🗑 Удалить сервер", callback_data=f"srv:xd:{acc_id}:{cb.data.split(':')[3]}")],
        back(f"srv:a:{acc_id}", "⬅️ К аккаунту")))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^srv:xd:\d+:\d+$"))
async def foreign_confirm(cb: CallbackQuery):
    acc_id, s = _foreign(cb.from_user.id, cb.data)
    if not s:
        return await cb.answer("🔄 Список устарел — обновите", show_alert=True)
    ip = s.ipv4[0] if s.ipv4 else s.name
    await cb.message.edit_text(
        f"🗑 <b>Удалить сервер</b> <code>{hd.quote(ip)}</code>?\n\n⚠️ Он будет удалён у провайдера безвозвратно."
        + ("" if s.name.startswith(BOT_NAMES) else "\n❗ Сервер создан не ботом — проверьте, что он вам не нужен."),
        reply_markup=kb([Btn(text="✅ Да, удалить", callback_data=f"srv:xy:{acc_id}:{cb.data.split(':')[3]}"),
                         Btn(text="❌ Нет", callback_data=f"srv:a:{acc_id}")]))
    await cb.answer()


async def _delete_live(uid: int, acc: db.Account, items: list[Server]) -> list[tuple[Server, str]]:
    failed = []
    async with make_client(acc) as client:
        for s in items:
            try:
                await client.delete_server(s.region, s.id)
            except Exception as e:
                failed.append((s, str(e)))
    return failed


@router.callback_query(F.data.regexp(r"^srv:xy:\d+:\d+$"))
async def foreign_delete(cb: CallbackQuery):
    acc_id, s = _foreign(cb.from_user.id, cb.data)
    acc = await db.get_account(cb.from_user.id, acc_id)
    if not s or not acc:
        return await cb.answer("🔄 Список устарел — обновите", show_alert=True)
    ip = s.ipv4[0] if s.ipv4 else s.name
    await cb.answer()
    await cb.message.edit_text(f"⏳ Удаляю <code>{hd.quote(ip)}</code>…")
    failed = await _delete_live(cb.from_user.id, acc, [s])
    text = (f"❌ <b>Не удалось удалить</b> <code>{hd.quote(ip)}</code>\n\n<code>{hd.quote(failed[0][1][:300])}</code>"
            if failed else f"✅ Сервер <code>{hd.quote(ip)}</code> удалён.")
    await cb.message.edit_text(text, reply_markup=kb(back(f"srv:a:{acc_id}", "⬅️ К аккаунту")))


@router.callback_query(F.data.regexp(r"^srv:cl:\d+$"))
@router.callback_query(F.data.regexp(r"^srv:cly:\d+$"))
async def cleanup_leftovers(cb: CallbackQuery):
    uid = cb.from_user.id
    acc = await db.get_account(uid, int(cb.data.split(":")[2]))
    if not acc:
        return await cb.answer("Аккаунт не найден", show_alert=True)
    ours = {r.server_id for r in await db.list_servers(uid, limit=1000) if r.account_id == acc.id}
    leftovers = [s for s in LIVE.get((uid, acc.id)) or [] if _leftover(s, ours)]
    if not leftovers:
        return await cb.answer("🧹 Остатков нет", show_alert=True)
    if cb.data.startswith("srv:cl:"):
        await cb.message.edit_text(
            f"🧹 <b>Удалить остатки бота ({len(leftovers)})?</b>\n\n"
            + "\n".join(f"• <code>{s.ipv4[0] if s.ipv4 else s.name}</code> · {hd.quote(s.name)}" for s in leftovers[:30])
            + "\n\nЭто пробные серверы, которые бот не смог удалить. Их паролей нет, они только тратят деньги.",
            reply_markup=kb([Btn(text="✅ Да, удалить", callback_data=f"srv:cly:{acc.id}"),
                             Btn(text="❌ Нет", callback_data=f"srv:a:{acc.id}")]))
        return await cb.answer()
    await cb.answer()
    await cb.message.edit_text(f"⏳ Удаляю {len(leftovers)} серверов…")
    failed = await _delete_live(uid, acc, leftovers)
    text = f"✅ Удалено: <b>{len(leftovers) - len(failed)}</b> из {len(leftovers)}"
    if failed:
        text += "\n\n" + "\n".join(f"❌ <code>{s.ipv4[0] if s.ipv4 else s.name}</code>: {hd.quote(e[:100])}"
                                      for s, e in failed)
    await cb.message.edit_text(text[:4000], reply_markup=kb(back(f"srv:a:{acc.id}", "⬅️ К аккаунту")))


@router.callback_query(F.data.startswith("srv:p:"))
async def provider_servers(cb: CallbackQuery):
    """Servers saved by the bot whose account was removed (and old buttons by provider)."""
    prov = cb.data.split(":", 2)[2]
    rows = (await _by_provider(cb.from_user.id)).get(prov, [])
    if not rows:
        return await servers(cb)
    text = (f"☁️ <b>{_name(prov)}</b> · {len(rows)}\n\n" + "\n".join(_line(r) for r in rows)
            + "\n\n👇 Откройте сервер, чтобы увидеть пароль или удалить его.")
    buttons = [[Btn(text=f"🖥 {r.ip}  ·  📍 {r.region}", callback_data=f"srv:{r.id}")] for r in rows]
    buttons.append([Btn(text=f"🗑 Удалить все {_name(prov)}"[:60], callback_data=f"srv:dp:{prov}")])
    await cb.message.edit_text(text[:4000], reply_markup=kb(*buttons, back("servers", "⬅️ Все серверы")))
    await cb.answer()


async def _card(user_id: int, row: db.ServerRow) -> tuple[str, object]:
    acc = await db.get_account(user_id, row.account_id)
    provider = acc.provider if acc else ""
    text = f"🖥 <b>Сервер {hd.quote(row.name)}</b>\n\n🌐 IP: <code>{row.ip}</code>\n"
    if row.admin_pass:
        text += f"🔑 Пароль root: <code>{hd.quote(row.admin_pass)}</code>\n"
    else:
        text += "🗝 Вход по SSH-ключу (пароль не задан)\n"
    text += (f"💻 SSH: <code>ssh root@{row.ip}</code>\n"
             f"📍 Локация: {hd.quote(row.region)}"
             f"{' · ' + PROVIDER_NAMES[provider] if provider in PROVIDER_NAMES else ''}\n"
             f"🗓 Куплен: {row.created_at[:16]}")
    rows = []
    if provider == "hostvds":
        text += ("\n\n🛠 <i>Не пускает по SSH? «Восстановить доступ» откроет файрвол и переустановит систему "
                 "с новым паролем. IP останется тем же.</i>")
        rows.append([Btn(text="🛠 Восстановить доступ", callback_data=f"srv:fix:{row.id}")])
    rows.append([Btn(text="🗑 Удалить сервер", callback_data=f"srv:del:{row.id}")])
    rows.append(back(f"srv:a:{acc.id}" if acc else f"srv:p:{OTHER}", "⬅️ К аккаунту" if acc else "⬅️ Назад"))
    return text, kb(*rows)


@router.callback_query(F.data.regexp(r"^srv:\d+$"))
async def server_card(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[1]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    text, markup = await _card(cb.from_user.id, row)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


# ---------- repair access (HostVDS servers bought before the firewall/password fix) ----------
@router.callback_query(F.data.regexp(r"^srv:fix:\d+$"))
async def confirm_fix(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[2]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    await cb.message.edit_text(
        f"🛠 <b>Восстановить доступ к</b> <code>{row.ip}</code>?\n\n"
        "1️⃣ Откроем входящие подключения (группа безопасности <code>allow_all</code>, как в панели HostVDS).\n"
        "2️⃣ Переустановим ту же систему с новым паролем root.\n\n"
        "🌐 IP останется прежним.\n⚠️ Все данные на диске сервера будут стёрты.",
        reply_markup=kb([Btn(text="✅ Да, восстановить", callback_data=f"srv:fixyes:{row.id}"),
                         Btn(text="❌ Нет", callback_data=f"srv:{row.id}")]))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^srv:fixyes:\d+$"))
async def do_fix(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[2]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    acc = await db.get_account(cb.from_user.id, row.account_id)
    if not acc:
        return await cb.answer("Аккаунт этого сервера удалён", show_alert=True)
    await cb.message.edit_text(f"⏳ Восстанавливаю доступ к <code>{row.ip}</code>…")
    await cb.answer()
    try:
        async with make_client(acc) as client:
            password = await client.repair_access(row.region, row.server_id)
    except Exception as e:
        log.exception("repair failed")
        await cb.message.edit_text(f"❌ <b>Не получилось</b>\n\n<code>{hd.quote(str(e)[:400])}</code>",
                                   reply_markup=kb(back(f"srv:{row.id}", "⬅️ К серверу")))
        return
    await db.update_server_password(cb.from_user.id, row.id, password)
    await cb.message.edit_text(
        f"✅ <b>Готово!</b> Сервер переустанавливается.\n\n"
        f"🌐 IP: <code>{row.ip}</code>\n🔑 Новый пароль root: <code>{hd.quote(password)}</code>\n"
        f"💻 SSH: <code>ssh root@{row.ip}</code>\n\n"
        "⏱ Подключайтесь через 1–2 минуты, когда система загрузится.",
        reply_markup=kb(back(f"srv:{row.id}", "⬅️ К серверу")))


# ---------- delete ----------
@router.callback_query(F.data.regexp(r"^srv:del:\d+$"))
async def confirm_delete(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[2]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    await cb.message.edit_text(
        f"🗑 <b>Удалить сервер</b> <code>{row.ip}</code>?\n\n⚠️ Он будет удалён у провайдера безвозвратно.",
        reply_markup=kb([Btn(text="✅ Да, удалить", callback_data=f"srv:yes:{row.id}"),
                         Btn(text="❌ Нет", callback_data=f"srv:{row.id}")]))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^srv:yes:\d+$"))
async def do_delete(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[2]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    await cb.message.edit_text(f"⏳ Удаляю <code>{row.ip}</code>…")
    await cb.answer()
    error = await _delete(cb.from_user.id, row)
    text = (f"❌ <b>Не удалось удалить</b> <code>{row.ip}</code>\n\n<code>{hd.quote(error[:300])}</code>" if error
            else f"✅ Сервер <code>{row.ip}</code> удалён.")
    await cb.message.edit_text(text, reply_markup=kb(back("servers", "☁️ К серверам")))


@router.callback_query(F.data == "srv:delall")
async def confirm_delete_all(cb: CallbackQuery):
    rows = await db.list_servers(cb.from_user.id, limit=1000)
    await cb.message.edit_text(
        f"🗑 <b>Удалить все серверы от бота ({len(rows)})?</b>\n\n⚠️ Они будут удалены у провайдера безвозвратно.",
        reply_markup=kb([Btn(text="✅ Да, удалить все", callback_data="srv:delall:yes"),
                         Btn(text="❌ Нет", callback_data="servers")]))
    await cb.answer()


@router.callback_query(F.data.startswith("srv:dp:"))
async def confirm_delete_provider(cb: CallbackQuery):
    prov = cb.data.split(":", 2)[2]
    rows = (await _by_provider(cb.from_user.id)).get(prov, [])
    await cb.message.edit_text(
        f"🗑 <b>Удалить все серверы {_name(prov)} ({len(rows)})?</b>\n\n⚠️ Они будут удалены у провайдера "
        "безвозвратно.",
        reply_markup=kb([Btn(text="✅ Да, удалить", callback_data=f"srv:dpy:{prov}"),
                         Btn(text="❌ Нет", callback_data=f"srv:p:{prov}")]))
    await cb.answer()


@router.callback_query(F.data == "srv:delall:yes")
@router.callback_query(F.data.startswith("srv:dpy:"))
async def do_delete_all(cb: CallbackQuery):
    if cb.data.startswith("srv:dpy:"):
        rows = (await _by_provider(cb.from_user.id)).get(cb.data.split(":", 2)[2], [])
    else:
        rows = await db.list_servers(cb.from_user.id, limit=1000)
    await cb.message.edit_text(f"⏳ Удаляю {len(rows)} серверов…")
    await cb.answer()
    errors = await asyncio.gather(*(_delete(cb.from_user.id, r) for r in rows))
    failed = [(r, e) for r, e in zip(rows, errors) if e]
    text = f"✅ Удалено: <b>{len(rows) - len(failed)}</b> из {len(rows)}"
    if failed:
        text += "\n\n" + "\n".join(f"❌ <code>{r.ip}</code>: {hd.quote(e[:100])}" for r, e in failed)
    await cb.message.edit_text(text[:4000], reply_markup=kb(back("servers", "☁️ К серверам")))
