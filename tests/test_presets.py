"""Admin-chosen subnets: the admin saves subnets per provider + location, users buy from them without
seeing a subnet picker (or are told to contact the admin); "My servers" is grouped by provider."""
import asyncio
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiohttp.test_utils import TestServer

from bot import db
from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")
from bot.handlers import accounts, buy, menu, servers, update  # noqa: E402
from tests.test_ip_select import buttons, fake_hostvds  # noqa: E402
from tests.test_servers_update import UID, FakeSession  # noqa: E402

USER = 77
REGION = {"regions": ["eu-west2"], "region_labels": {"eu-west2": "🇳🇱 Amsterdam · eu-west2"}}


def cb(n, data, uid):
    user = User(id=uid, is_bot=False, first_name="A")
    msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=user, chat_instance="c",
                                                             message=msg, data=data))


def cbs(session):
    return [b.callback_data for b in buttons(session)]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    srv = TestServer(fake_hostvds())
    await srv.start_server()
    object.__setattr__(config, "hostvds_auth_url", str(srv.make_url("/identity")))
    try:
        admin_acc = await db.add_account(UID, "hostvds", "u", "p", "u")
        user_acc = await db.add_account(USER, "hostvds", "u2", "p2", "u2")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        for r in (menu.router, accounts.router, servers.router, buy.router, update.router):
            r._parent_router = None
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
        actx, uctx = dp.fsm.get_context(bot, UID, UID), dp.fsm.get_context(bot, USER, USER)

        # 1) user, nothing saved yet → no picker, "contact the admin"
        await dp.feed_update(bot, cb(1, "buy", USER))
        await uctx.update_data(provider="hostvds", account_id=user_acc, **REGION)
        await dp.feed_update(bot, cb(2, "buy:reg:0", USER))
        text = s.texts()[-1]
        assert "Не удалось найти рабочую подсеть" in text and "Свяжитесь с админом" in text, text
        assert "buy:ipok" not in cbs(s) and not any(c and c.startswith("buy:ipt:") for c in cbs(s))
        assert any(b.url == "https://t.me/oldarsi" for b in buttons(s))

        # 2) admin picks subnets and saves them for users
        await dp.feed_update(bot, cb(3, "buy", UID))
        await actx.update_data(provider="hostvds", account_id=admin_acc, **REGION)
        await dp.feed_update(bot, cb(4, "buy:reg:0", UID))
        assert "ещё не заданы" in s.texts()[-1] and "buy:ipsave" not in cbs(s)
        await dp.feed_update(bot, cb(5, "buy:ipt:4", UID))   # 95.182.87
        await dp.feed_update(bot, cb(6, "buy:ipt:0", UID))   # 94.103.80
        assert "buy:ipsave" in cbs(s)
        await dp.feed_update(bot, cb(7, "buy:ipsave", UID))
        assert await db.get_preset("hostvds", "eu-west2") == ["94.103.80", "95.182.87"]
        assert "Для пользователей сохранено: <b>2</b>" in s.texts()[-1] and "buy:ipunsave" in cbs(s)

        # a user can't save
        await dp.feed_update(bot, cb(8, "buy:ipsave", USER))
        assert await db.get_preset("hostvds", "eu-west2") == ["94.103.80", "95.182.87"]

        # 3) the admin opens the location again: the saved subnets come pre-selected (to edit them)
        await dp.feed_update(bot, cb(9, "buy:reg:0", UID))
        assert "Выбрано: <b>2</b>" in s.texts()[-1] and "✅ 95.182.87.#" in [b.text for b in buttons(s)]
        await dp.feed_update(bot, cb(10, "buy:ipt:0", UID))  # drop 94.103.80
        await dp.feed_update(bot, cb(11, "buy:ipsave", UID))
        assert await db.get_preset("hostvds", "eu-west2") == ["95.182.87"]

        # locations with saved subnets get a ⭐ and come first
        regs = [("eu-west1", "🇩🇪 Frankfurt · eu-west1", True, ""), ("eu-west2", "🇳🇱 Amsterdam · eu-west2", True, "")]
        orig_load = buy._load_regions

        async def fake_load(cb_, state_, provider):
            return regs
        buy._load_regions = fake_load
        try:
            await uctx.update_data(provider="hostvds")
            await dp.feed_update(bot, cb(30, f"buy:acc:{user_acc}", USER))
        finally:
            buy._load_regions = orig_load
        labels = [b.text for b in buttons(s)]
        assert labels[:2] == ["🇳🇱 Amsterdam · eu-west2 ⭐", "🇩🇪 Frankfurt · eu-west1"], labels
        assert "⭐ — здесь есть рабочие подсети" in s.texts()[-1]
        assert (await uctx.get_data())["regions"][0] == "eu-west2"

        # 4) user: goes straight to the count step, subnets used but not shown
        await dp.feed_update(bot, cb(12, "buy", USER))
        await uctx.update_data(provider="hostvds", account_id=user_acc, **REGION)
        await dp.feed_update(bot, cb(13, "buy:reg:0", USER))
        text = s.texts()[-1]
        assert "Шаг 3.</b> Сколько серверов" in text and "подобраны админом" in text and "95.182" not in text, text
        data = await uctx.get_data()
        assert data["prefixes"] == ["95.182.87"] and data["targets"] == [["pub", "s1"]] and data["network_id"] == "pub"
        job_text = buy._progress_text(buy.PurchaseJob("eu-west2", None, "i", None, ["95.182.87"], 1, 0, 1, 5, 10,
                                                      hide_prefixes=True))
        assert "95.182" not in job_text and "подобраны админом" in job_text

        # 5) admin list of saved subnets, delete → users are told to contact the admin again
        await dp.feed_update(bot, cb(14, "presets", UID))
        assert "Amsterdam" in s.texts()[-1] and "<code>95.182.87.#</code>" in s.texts()[-1]
        before = len(s.texts())
        await dp.feed_update(bot, cb(15, "presets", USER))
        assert len(s.texts()) == before, "users can't open the admin's subnet list"
        await dp.feed_update(bot, cb(16, "pst:del:0", UID))
        assert await db.get_preset("hostvds", "eu-west2") == [] and "ничего не сохранено" in s.texts()[-1]

        # 6) "My servers": grouped by provider
        do_acc = await db.add_account(UID, "digitalocean", "do", "t", "")
        await db.add_server(UID, admin_acc, "eu-west2", "a1", "vps-a", "95.182.87.5", "pw")
        await db.add_server(UID, do_acc, "ams3", "d1", "vps-d", "188.166.1.1", "pw")
        await db.add_server(UID, admin_acc, "eu-west2", "a2", "vps-b", "95.182.87.6", "pw")
        await dp.feed_update(bot, cb(17, "servers", UID))
        text = s.texts()[-1]
        assert text.index("HostVDS</b> · 2") < text.index("DigitalOcean</b> · 1"), text
        assert {"srv:p:hostvds", "srv:p:digitalocean"} <= set(cbs(s))
        await dp.feed_update(bot, cb(18, "srv:p:digitalocean", UID))
        assert "188.166.1.1" in s.texts()[-1] and "95.182.87.5" not in s.texts()[-1]
        assert "srv:dp:digitalocean" in cbs(s)
        await dp.feed_update(bot, cb(19, "srv:p:hostvds", UID))
        card = next(c for c in cbs(s) if c.startswith("srv:") and c[4:].isdigit())
        await dp.feed_update(bot, cb(20, card, UID))
        assert "srv:p:hostvds" in cbs(s), "the card leads back to its provider"
    finally:
        await srv.close()


def test_presets():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_presets()
    print("OK")
