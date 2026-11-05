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

    # 1) a stranger sees the presentation with their ID, not the menu
    await dp.feed_update(bot, msg(GUEST, "/start"))
    landing = s.texts()[-1]
    print(landing, "\n")
    assert "бот для покупки VPS-серверов у разных провайдеров" in landing and "@oldarsi" in landing
    assert "<code>2002</code>" in landing, "the guest's ID is shown so they can send it to the admin"
    assert "HostVDS" not in landing and "Selectel" not in landing, "providers are not listed"
    buttons = last_markup(s)
    assert len(buttons) == 1 and buttons[0].url == "https://t.me/oldarsi" and buttons[0].text == "💬 Написать админу"
    # any button / command is gated
    await dp.feed_update(bot, cb(GUEST, "buy"))
    assert isinstance(s.calls[-1], EditMessageText) and "Доступ только по приглашению" in s.calls[-1].text
    await dp.feed_update(bot, cb(GUEST, "servers"))
    assert not any("Мои серверы" in t for t in s.texts())
    await dp.feed_update(bot, cb(GUEST, "access:req"))  # old button from a previous version does nothing
    assert not sent_to(s, ADMIN)

    # 2) admin grants access by ID → guest is notified and gets the real menu
    await dp.feed_update(bot, cb(ADMIN, "users"))
    assert "➕ Добавить по ID" in [b.text for b in last_markup(s)]
    await dp.feed_update(bot, cb(ADMIN, "usr:add"))
    await dp.feed_update(bot, msg(ADMIN, "2002"))
    assert "Доступ для <code>2002</code> выдан" in s.texts()[-1]
    assert "Доступ открыт" in sent_to(s, GUEST)[-1].text
    assert await access.has_access(GUEST)
    await dp.feed_update(bot, msg(GUEST, "/start"))
    assert "🛒 Купить сервер" in [b.text for b in last_markup(s)]
    assert "👥 Пользователи" not in " ".join(b.text for b in last_markup(s)), "no admin buttons for users"
    await dp.feed_update(bot, cb(GUEST, "users"))
    assert isinstance(s.calls[-1], AnswerCallbackQuery) and "Только для админов" in s.calls[-1].text
    await dp.feed_update(bot, msg(GUEST, "/start"))
    assert "🛒 Купить сервер" in [b.text for b in last_markup(s)]

    # 3) admin list + revoke
    await dp.feed_update(bot, cb(ADMIN, "users"))
    assert "С доступом: 1" in s.texts()[-1]
    await dp.feed_update(bot, cb(ADMIN, "usr:rm:2002"))
    assert "Доступ к боту закрыт" in sent_to(s, GUEST)[-1].text
    assert not await access.has_access(GUEST)
    await dp.feed_update(bot, msg(GUEST, "/start"))
    assert "Доступ только по приглашению" in s.texts()[-1]

    # 4) bad ID input
    await dp.feed_update(bot, cb(ADMIN, "usr:add"))
    await dp.feed_update(bot, msg(ADMIN, "abc"))
    assert "Нужен числовой ID" in s.texts()[-1]


def test_access():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_access()
    print("OK")
