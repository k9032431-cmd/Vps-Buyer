import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db
from ..keyboards import back, kb
from .accounts import make_client

router = Router()


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
        await cb.message.edit_text("☁️ <b>Мои серверы</b>\n\nПока пусто. Нажмите «🛒 Купить сервер» в меню.",
                                   reply_markup=kb(back()))
        return await cb.answer()
    buttons = [[Btn(text=f"🖥 {r.ip} · {r.region}", callback_data=f"srv:{r.id}")] for r in rows]
    buttons.append([Btn(text="🗑 Удалить все", callback_data="srv:delall")])
    ips = "\n".join(f"<code>{r.ip}</code> · {hd.quote(r.region)}" for r in rows)
    await cb.message.edit_text(f"☁️ <b>Мои серверы</b> ({len(rows)})\n\n{ips}\n\n"
                               "Нажмите на IP, чтобы скопировать. Кнопка ниже — посмотреть или удалить:",
                               reply_markup=kb(*buttons, back()))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^srv:\d+$"))
async def server_card(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[1]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    text = (f"🖥 <b>{hd.quote(row.name)}</b>\n\n"
            f"IP: <code>{row.ip}</code>\n"
            f"Локация: {hd.quote(row.region)}\n"
            f"Куплен: {row.created_at[:16]}\n")
    if row.admin_pass:
        text += f"Пароль root: <tg-spoiler>{hd.quote(row.admin_pass)}</tg-spoiler>\n"
    await cb.message.edit_text(text, reply_markup=kb(
        [Btn(text="🗑 Удалить сервер", callback_data=f"srv:del:{row.id}")], back("servers")))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^srv:del:\d+$"))
async def confirm_delete(cb: CallbackQuery):
    row = await db.get_server(cb.from_user.id, int(cb.data.split(":")[2]))
    if not row:
        return await cb.answer("Сервер не найден", show_alert=True)
    await cb.message.edit_text(
        f"⚠️ Удалить сервер <code>{row.ip}</code>?\n\nОн будет удалён у провайдера безвозвратно.",
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
    text = f"❌ Не удалось удалить <code>{row.ip}</code>: {hd.quote(error[:300])}" if error \
        else f"✅ Сервер <code>{row.ip}</code> удалён."
    await cb.message.edit_text(text, reply_markup=kb(back("servers", "☁️ К серверам")))


@router.callback_query(F.data == "srv:delall")
async def confirm_delete_all(cb: CallbackQuery):
    rows = await db.list_servers(cb.from_user.id)
    await cb.message.edit_text(
        f"⚠️ Удалить <b>все {len(rows)}</b> серверов у провайдера безвозвратно?",
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
    text = f"✅ Удалено: {len(rows) - len(failed)} из {len(rows)}"
    if failed:
        text += "\n\n" + "\n".join(f"❌ <code>{r.ip}</code>: {hd.quote(e[:100])}" for r, e in failed)
    await cb.message.edit_text(text[:4000], reply_markup=kb(back("servers", "☁️ К серверам")))
