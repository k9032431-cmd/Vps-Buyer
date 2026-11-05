import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..keyboards import PROVIDERS, back, kb
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


@router.callback_query(F.data == "servers")
async def servers(cb: CallbackQuery):
    rows = await db.list_servers(cb.from_user.id)
    if not rows:
        await cb.message.edit_text(
            "☁️ <b>Мои серверы</b>\n\n📭 Пока пусто.\nНажмите «🛒 Купить сервер» в главном меню.",
            reply_markup=kb([Btn(text="🛒 Купить сервер", callback_data="buy")], back("menu", "🏠 Главное меню")))
        return await cb.answer()
    buttons = [[Btn(text=f"🖥 {r.ip}  ·  📍 {r.region}", callback_data=f"srv:{r.id}")] for r in rows]
    buttons.append([Btn(text="🗑 Удалить все", callback_data="srv:delall")])
    ips = "\n".join(f"🖥 <code>{r.ip}</code>  ·  📍 {hd.quote(r.region)}" for r in rows)
    await cb.message.edit_text(f"☁️ <b>Мои серверы</b> · {len(rows)}\n\n{ips}\n\n"
                               "💡 Нажмите на IP, чтобы скопировать.\n👇 Откройте сервер, чтобы увидеть пароль "
                               "или удалить его.",
                               reply_markup=kb(*buttons, back("menu", "🏠 Главное меню")))
    await cb.answer()


async def _card(user_id: int, row: db.ServerRow) -> tuple[str, object]:
    acc = await db.get_account(user_id, row.account_id)
    provider = acc.provider if acc else ""
    text = f"🖥 <b>Сервер {hd.quote(row.name)}</b>\n\n🌐 IP: <code>{row.ip}</code>\n"
    if row.admin_pass:
        text += f"🔑 Пароль root: <code>{hd.quote(row.admin_pass)}</code>\n"
    text += (f"💻 SSH: <code>ssh root@{row.ip}</code>\n"
             f"📍 Локация: {hd.quote(row.region)}"
             f"{' · ' + PROVIDERS[provider] if provider in PROVIDERS else ''}\n"
             f"🗓 Куплен: {row.created_at[:16]}")
    rows = []
    if provider == "hostvds":
        text += ("\n\n🛠 <i>Не пускает по SSH? «Восстановить доступ» откроет файрвол и переустановит систему "
                 "с новым паролем. IP останется тем же.</i>")
        rows.append([Btn(text="🛠 Восстановить доступ", callback_data=f"srv:fix:{row.id}")])
    rows.append([Btn(text="🗑 Удалить сервер", callback_data=f"srv:del:{row.id}")])
    rows.append(back("servers", "⬅️ К списку серверов"))
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
    rows = await db.list_servers(cb.from_user.id)
    await cb.message.edit_text(
        f"🗑 <b>Удалить все серверы ({len(rows)})?</b>\n\n⚠️ Они будут удалены у провайдера безвозвратно.",
        reply_markup=kb([Btn(text="✅ Да, удалить все", callback_data="srv:delall:yes"),
                         Btn(text="❌ Нет", callback_data="servers")]))
    await cb.answer()


@router.callback_query(F.data == "srv:delall:yes")
async def do_delete_all(cb: CallbackQuery):
    rows = await db.list_servers(cb.from_user.id)
    await cb.message.edit_text(f"⏳ Удаляю {len(rows)} серверов…")
    await cb.answer()
    errors = await asyncio.gather(*(_delete(cb.from_user.id, r) for r in rows))
    failed = [(r, e) for r, e in zip(rows, errors) if e]
    text = f"✅ Удалено: <b>{len(rows) - len(failed)}</b> из {len(rows)}"
    if failed:
        text += "\n\n" + "\n".join(f"❌ <code>{r.ip}</code>: {hd.quote(e[:100])}" for r, e in failed)
    await cb.message.edit_text(text[:4000], reply_markup=kb(back("servers", "☁️ К серверам")))
