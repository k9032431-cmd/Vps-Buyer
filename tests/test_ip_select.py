"""Multi-select IP screen through the bot handlers (fake Telegram + fake HostVDS)."""
import asyncio
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db
from bot.config import config

# keep tests offline: the HostVDS site API is unreachable → OpenStack fallback is used
object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")
from bot.handlers import accounts, buy, menu, servers, update
from tests.test_servers_update import UID, FakeSession, cb_update


def fake_hostvds():
    a = web.Application()

    async def tokens(req):
        base = str(req.url.origin())
        cat = [{"type": t, "endpoints": [{"interface": "public", "region_id": "eu-west2", "url": f"{base}/{t}"}]}
               for t in ("compute", "network", "image")]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}}, status=201,
                                 headers={"X-Subject-Token": "t"})
    a.router.add_post("/identity/v3/auth/tokens", tokens)
    a.router.add_get("/network/v2.0/networks", lambda r: web.json_response({"networks": [{"id": "pub", "shared": True}]}))
    a.router.add_get("/network/v2.0/subnets", lambda r: web.json_response({"subnets": [
        {"id": "s1", "network_id": "pub", "ip_version": 4, "cidr": "95.182.84.0/22",
         "allocation_pools": [{"start": "95.182.84.2", "end": "95.182.87.254"}]},
        {"id": "s2", "network_id": "pub", "ip_version": 4, "cidr": "94.103.80.0/24"}]}))
    a.router.add_get("/image/v2/images", lambda r: web.json_response(
        {"images": [{"id": "u22", "name": "Ubuntu 22.04 LTS"}]}))
    return a


def buttons(session):
    last = next(c for c in reversed(session.calls) if getattr(c, "reply_markup", None))
    return [b for row in last.reply_markup.inline_keyboard for b in row]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    srv = TestServer(fake_hostvds())
    await srv.start_server()
    object.__setattr__(config, "hostvds_auth_url", str(srv.make_url("/identity")))
    try:
        acc = await db.add_account(UID, "hostvds", "u", "p", "u")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "hostvds", "account_id": acc, "regions": ["eu-west2"],
                            "region_labels": {"eu-west2": "🇳🇱 Amsterdam · eu-west2"},
                            "flavors": [{"id": "f", "name": "f", "vcpus": 1, "ram_mb": 1024, "disk_gb": 10, "price": ""}]})
        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        # blocks: 94.103.80, 95.182.84, 95.182.85, 95.182.86, 95.182.87 ; no "Готово" until something is picked
        assert [b.text for b in buttons(s)][:5] == ["94.103.80.#", "95.182.84.#", "95.182.85.#", "95.182.86.#",
                                                   "95.182.87.#"]
        assert not any(b.callback_data == "buy:ipok" for b in buttons(s))

        await dp.feed_update(bot, cb_update(2, "buy:ipt:4"))   # tick 95.182.87
        await dp.feed_update(bot, cb_update(3, "buy:ipt:0"))   # tick 94.103.80
        await dp.feed_update(bot, cb_update(4, "buy:ipt:0"))   # untick 94.103.80
        await dp.feed_update(bot, cb_update(5, "buy:ipt:1"))   # tick 95.182.84
        assert "✅ 95.182.87.#" in [b.text for b in buttons(s)] and "Выбрано: <b>2</b>" in s.texts()[-1]

        # manual entry of several prefixes: one is a listed block (gets ticked), one is new
        await dp.feed_update(bot, cb_update(6, "buy:ip:custom"))
        user = User(id=UID, is_bot=False, first_name="A")
        msg = Message(message_id=9, date=datetime.now(), chat=Chat(id=UID, type="private"), from_user=user,
                      text="94.103.80, 5.188")
        await dp.feed_update(bot, Update(update_id=7, message=msg))
        assert "Выбрано: <b>4</b>" in s.texts()[-1] and "<code>5.188.#.#</code>" in s.texts()[-1], s.texts()[-1]

        await dp.feed_update(bot, cb_update(8, "buy:ipok"))
        data = await ctx.get_data()
        assert data["prefixes"] == ["94.103.80", "95.182.84", "95.182.87", "5.188"], data["prefixes"]
        assert data["targets"] == [["pub", "s2"], ["pub", "s1"], ["pub", "s1"], [None, None]], data["targets"]
        assert "<code>95.182.87.#</code>" in s.texts()[-1]

        await dp.feed_update(bot, cb_update(9, "buy:cnt:2"))
        await dp.feed_update(bot, cb_update(10, "buy:fl:0"))
        await dp.feed_update(bot, cb_update(11, "buy:img:0"))
        confirm = s.texts()[-1]
        print(confirm)
        assert "Нужные IP: <code>94.103.80.#</code>, <code>95.182.84.#</code>, <code>95.182.87.#</code>, " \
               "<code>5.188.#.#</code>" in confirm

        # buy: every found server is sent right away and saved to "My servers"
        from bot.providers import Server

        async def fake_run(client, job, progress):
            for i, ip in enumerate(["95.182.87.5", "94.103.80.9"]):
                await asyncio.sleep(0)
                job.kept.append(Server(f"s{i}", f"vps-{i}", "ACTIVE", [ip], "pw" + str(i)))
                await job.on_found(job.kept[-1])
            return job
        orig = buy.run_purchase
        buy.run_purchase = fake_run
        try:
            await dp.feed_update(bot, cb_update(20, "buy:go"))
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            buy.run_purchase = orig
        texts = s.texts()
        found_msgs = [t for t in texts if t.startswith("🎉 <b>Найден IP</b>")]
        assert len(found_msgs) == 2 and "<code>95.182.87.5</code>" in found_msgs[0] and "Готово: 1/2" in found_msgs[0]
        assert "<tg-spoiler>pw0</tg-spoiler>" in found_msgs[0]
        assert any("Итого куплено 2" in t for t in texts)
        assert [r.ip for r in await db.list_servers(UID)] == ["94.103.80.9", "95.182.87.5"]

        # "select all" + reset (state was cleared by the purchase)
        await ctx.set_data({"provider": "hostvds", "account_id": acc, "regions": ["eu-west2"],
                            "region_labels": {"eu-west2": "🇳🇱 Amsterdam · eu-west2"}})
        await dp.feed_update(bot, cb_update(12, "buy:reg:0"))
        await dp.feed_update(bot, cb_update(13, "buy:ipall"))
        assert "Выбрано: <b>5</b>" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(14, "buy:ipclr"))
        assert "Выбрано: <b>0</b>" in s.texts()[-1]
    finally:
        await srv.close()


def test_ip_select():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_ip_select()
    print("OK")
