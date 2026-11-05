"""Invite-only access: landing for strangers, request → admin approves/denies/revokes."""
import asyncio
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from tests.test_servers_update import FakeSession  # noqa: E402

ADMIN, GUEST = 1001, 2002
_n = [0]


def _user(uid):
    return User(id=uid, is_bot=False, first_name="Admin" if uid == ADMIN else "Guest", username=f"u{uid}")


def msg(uid, text):
    _n[0] += 1
    m = Message(message_id=_n[0], date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=_user(uid),
                text=text)
    return Update(update_id=_n[0], message=m)


def cb(uid, data):
    _n[0] += 1
    m = Message(message_id=_n[0], date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
    return Update(update_id=_n[0], callback_query=CallbackQuery(id=str(_n[0]), from_user=_user(uid),
                                                                chat_instance="c", message=m, data=data))


def sent_to(session, chat_id):
    return [c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == chat_id]


def last_markup(session):
    c = next(c for c in reversed(session.calls) if getattr(c, "reply_markup", None))
    return [b for row in c.reply_markup.inline_keyboard for b in row]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {ADMIN})
    object.__setattr__(config, "admin_contact", "oldarsi")
    await db.init_db()
    s = FakeSession()
    bot = Bot("1:x", session=s)
    dp = Dispatcher()
    dp.update.outer_middleware(access.AccessMiddleware())
    dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)

    # 1) a stranger sees the presentation, not the menu
    await dp.feed_update(bot, msg(GUEST, "/start"))
    landing = s.texts()[-1]
    print(landing, "\n")
    assert "бот для покупки VPS-серверов у разных провайдеров" in landing and "@oldarsi" in landing
    buttons = last_markup(s)
    assert buttons[0].callback_data == "access:req" and buttons[1].url == "https://t.me/oldarsi"
    # any button / command is gated
    await dp.feed_update(bot, cb(GUEST, "buy"))
    assert isinstance(s.calls[-1], EditMessageText) and "Доступ только по приглашению" in s.calls[-1].text
    await dp.feed_update(bot, cb(GUEST, "servers"))
    assert not any("Мои серверы" in t for t in s.texts())

    # 2) request access → every admin gets ✅/❌
    await dp.feed_update(bot, cb(GUEST, "access:req"))
    to_admin = sent_to(s, ADMIN)
    assert len(to_admin) == 1 and "Новый запрос доступа" in to_admin[0].text and "@u2002" in to_admin[0].text
    assert [b.callback_data for b in to_admin[0].reply_markup.inline_keyboard[0]] == ["usr:ok:2002", "usr:no:2002"]
    assert "Запрос отправлен" in s.texts()[-1]
    # second request right away → just an alert, admins are not spammed
    await dp.feed_update(bot, cb(GUEST, "access:req"))
    assert isinstance(s.calls[-1], AnswerCallbackQuery) and "уже отправлен" in s.calls[-1].text
    assert len(sent_to(s, ADMIN)) == 1

    # admin menu shows the pending request
    await dp.feed_update(bot, msg(ADMIN, "/start"))
    assert "👥 Пользователи · 🙋 1" in [b.text for b in last_markup(s)]

    # 3) admin approves → guest is notified and gets the real menu
    await dp.feed_update(bot, cb(ADMIN, "usr:ok:2002"))
    assert "Доступ выдан" in s.texts()[-2] or "Доступ выдан" in s.texts()[-1]
    assert "Доступ открыт" in sent_to(s, GUEST)[-1].text
    assert await access.has_access(GUEST)
    await dp.feed_update(bot, msg(GUEST, "/start"))
    assert "🛒 Купить сервер" in [b.text for b in last_markup(s)]
    assert "👥 Пользователи" not in " ".join(b.text for b in last_markup(s)), "no admin buttons for users"
    await dp.feed_update(bot, cb(GUEST, "users"))
    assert isinstance(s.calls[-1], AnswerCallbackQuery) and "Только для админов" in s.calls[-1].text

    # 4) admin list + revoke
    await dp.feed_update(bot, cb(ADMIN, "users"))
    assert "С доступом: 1" in s.texts()[-1]
    await dp.feed_update(bot, cb(ADMIN, "usr:rm:2002"))
    assert "Доступ к боту закрыт" in sent_to(s, GUEST)[-1].text
    assert not await access.has_access(GUEST)
    await dp.feed_update(bot, msg(GUEST, "/start"))
    assert "Доступ только по приглашению" in s.texts()[-1]

    # 5) grant by ID
    await dp.feed_update(bot, cb(ADMIN, "usr:add"))
    await dp.feed_update(bot, msg(ADMIN, "3003"))
    assert await access.has_access(3003) and "Доступ для <code>3003</code> выдан" in s.texts()[-1]

    # 6) deny path
    await dp.feed_update(bot, msg(4004, "/start"))
    await dp.feed_update(bot, cb(4004, "access:req"))
    await dp.feed_update(bot, cb(ADMIN, "usr:no:4004"))
    assert "В доступе отказано" in sent_to(s, 4004)[-1].text and not await access.has_access(4004)
    await dp.feed_update(bot, cb(4004, "access:req"))  # within the cooldown
    assert isinstance(s.calls[-1], AnswerCallbackQuery) and "отклонили" in s.calls[-1].text


def test_access():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_access()
    print("OK")
