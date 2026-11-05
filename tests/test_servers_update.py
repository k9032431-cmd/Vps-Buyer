"""Server deletion via Telegram handlers + self-update via git, with fake Telegram/OpenStack."""
import asyncio
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db, updater
from bot.config import config
from bot.handlers import accounts, buy, menu, servers, update

UID = 42


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=UID, type="private"), text=method.text)
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass

    def texts(self):
        return [c.text for c in self.calls if isinstance(c, (EditMessageText, SendMessage))]


def cb_update(n, data):
    user = User(id=UID, is_bot=False, first_name="A")
    msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=UID, type="private"), text="x")
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=user, chat_instance="c",
                                                             message=msg, data=data))


def fake_openstack(deleted):
    app = web.Application()

    async def tokens(req):
        cat = [{"type": "compute", "endpoints": [{"interface": "public", "region_id": "eu-west1",
                                                  "url": str(req.url.origin()) + "/compute"}]}]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}},
                                 status=201, headers={"X-Subject-Token": "t"})

    async def delete(req):
        sid = req.match_info["id"]
        deleted.append(sid)
        return web.Response(status=404 if sid == "gone" else 204)

    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_delete("/compute/servers/{id}", delete)
    return app


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


async def scenario():
    tmp = Path(tempfile.mkdtemp())
    object.__setattr__(config, "db_path", str(tmp / "t.db"))
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()

    deleted = []
    srv = TestServer(fake_openstack(deleted))
    await srv.start_server()
    object.__setattr__(config, "hostvds_auth_url", str(srv.make_url("/identity")))
    acc = await db.add_account(UID, "hostvds", "u", "p", "u")
    for sid, ip in (("a", "94.1.1.1"), ("b", "94.1.1.2"), ("gone", "94.1.1.3")):
        await db.add_server(UID, acc, "eu-west1", sid, "vps-" + sid, ip, "pw")

    session = FakeSession()
    bot = Bot("1:x", session=session)
    dp = Dispatcher()
    dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
    try:
        rows = await db.list_servers(UID)
        await dp.feed_update(bot, cb_update(1, "servers"))
        assert "Мои серверы</b> (3)" in session.texts()[-1]
        await dp.feed_update(bot, cb_update(2, f"srv:del:{rows[-1].id}"))   # server "a"
        assert "Удалить сервер <code>94.1.1.1" in session.texts()[-1]
        await dp.feed_update(bot, cb_update(3, f"srv:yes:{rows[-1].id}"))
        assert "удалён" in session.texts()[-1] and deleted == ["a"]
        await dp.feed_update(bot, cb_update(4, "srv:delall:yes"))           # b + already-gone server
        assert "Удалено: 2 из 2" in session.texts()[-1], session.texts()[-1]
        assert await db.list_servers(UID) == []
    finally:
        await srv.close()

    # ---- self-update ----
    origin, clone = tmp / "origin.git", tmp / "clone"
    git(tmp, "init", "-q", "--bare", str(origin))
    git(tmp, "clone", "-q", str(origin), str(clone))
    (clone / "requirements.txt").write_text("")
    git(clone, "add", "."); git(clone, "commit", "-qm", "v1"); git(clone, "push", "-q", "origin", "HEAD")
    updater.ROOT = clone
    assert (await updater.update()).updated is False          # nothing new yet

    other = tmp / "other"
    git(tmp, "clone", "-q", str(origin), str(other))
    (other / "f.txt").write_text("new"); git(other, "add", "."); git(other, "commit", "-qm", "Новая фича")
    git(other, "push", "-q", "origin", "HEAD")

    stop = []
    dp.stop_polling = lambda: asyncio.sleep(0, stop.append(1))
    await dp.feed_update(bot, cb_update(5, "update"), dispatcher=dp)
    assert stop == [1]
    text = "\n".join(session.texts()[-2:])
    assert "Бот обновлён" in text and "Новая фича" in text, text
    assert (clone / "f.txt").exists() and update.RESTART["update_id"] == 5
    assert updater.pop_notify()["chat_id"] == UID
    print("update message:", session.texts()[-1].splitlines()[0])


def test_servers_and_update():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_servers_and_update()
    print("OK")
