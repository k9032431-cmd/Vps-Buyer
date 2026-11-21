"""«Мои серверы»: every server in each connected account (live from the provider API), grouped by
provider and account — bot purchases with passwords, leftovers of the bot to clean up, foreign servers."""
import asyncio
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db
from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")
from bot.handlers import accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import vultr  # noqa: E402
from tests.test_ip_select import buttons  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402


def fake_vultr():
    st = {"instances": [
        {"id": "i-ours", "label": "vps-a1b2c3", "main_ip": "45.32.1.1", "region": "ewr", "status": "active",
         "power_status": "running"},
        {"id": "i-left", "label": "vps-dead00", "main_ip": "45.32.9.9", "region": "ams", "status": "pending",
         "power_status": "stopped"},
        {"id": "i-web", "label": "my-website", "main_ip": "149.28.5.5", "region": "fra", "status": "active",
         "power_status": "running"}], "deleted": []}
    a = web.Application()

    async def instances(req):
        return web.json_response({"instances": [i for i in st["instances"] if i["id"] not in st["deleted"]],
                                  "meta": {"links": {"next": ""}}})
    a.router.add_get("/v2/instances", instances)
    a.router.add_get("/v2/reserved-ips", lambda r: web.json_response({"reserved_ips": [], "meta": {"links": {"next": ""}}}))

    async def delete(req):
        st["deleted"].append(req.match_info["id"])
        return web.Response(status=204)
    a.router.add_delete("/v2/instances/{id}", delete)
    return a, st


def cbs(s):
    return [b.callback_data for b in buttons(s)]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    app, st = fake_vultr()
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "vultr_api_url", str(srv.make_url("/v2")))
    object.__setattr__(config, "hostvds_auth_url", "http://127.0.0.1:9/identity")  # unreachable → no list
    try:
        vacc = await db.add_account(UID, "vultr", "me@x.io", "t", "")
        hacc = await db.add_account(UID, "hostvds", "hv", "p", "hv")
        await db.add_server(UID, vacc, "ewr", "i-ours", "vps-a1b2c3", "45.32.1.1", "secret")
        await db.add_server(UID, vacc, "ewr", "i-gone", "vps-gone", "45.32.7.7", "pw")   # deleted in the panel
        await db.add_server(UID, hacc, "eu-west2", "h1", "vps-h", "95.182.87.5", "pw")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)

        # overview: every account with the number of servers really in it
        await dp.feed_update(bot, cb_update(1, "servers"))
        text = s.texts()[-1]
        print(text)
        assert text.index("HostVDS") < text.index("Vultr"), "providers in the bot's order"
        assert "me@x.io — в аккаунте: <b>3</b> · 🤖 от бота: 1" in text, text
        assert "hv — от бота: <b>1</b> (список аккаунта недоступен)" in text
        assert "Всего серверов: <b>4</b>" in text
        assert {f"srv:a:{vacc}", f"srv:a:{hacc}"} <= set(cbs(s))

        # one account: bot servers (cards with passwords), a leftover to clean, a foreign server, a gone one
        await dp.feed_update(bot, cb_update(2, f"srv:a:{vacc}"))
        text = s.texts()[-1]
        print(text)
        assert "В аккаунте: <b>3</b>" in text
        assert "🤖 <code>45.32.1.1</code>" in text and "🧹 <code>45.32.9.9</code>" in text
        assert "🖥 <code>149.28.5.5</code>" in text and "👻 <code>45.32.7.7</code>" in text
        ours = (await db.list_servers(UID, 1000))
        row = next(r for r in ours if r.server_id == "i-ours")
        assert f"srv:{row.id}" in cbs(s) and f"srv:cl:{vacc}" in cbs(s)

        # the bot's own server opens its card (password) and leads back to the account
        await dp.feed_update(bot, cb_update(3, f"srv:{row.id}"))
        assert "secret" in s.texts()[-1] and f"srv:a:{vacc}" in cbs(s)

        # a foreign server: card without password, delete with a warning
        await dp.feed_update(bot, cb_update(4, f"srv:x:{vacc}:2"))
        assert "создан не ботом" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(5, f"srv:xd:{vacc}:2"))
        assert "проверьте, что он вам не нужен" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(6, f"srv:xy:{vacc}:2"))
        assert st["deleted"] == ["i-web"] and "удалён" in s.texts()[-1]

        # leftovers of the bot: one button removes them all (only them)
        await dp.feed_update(bot, cb_update(7, f"srv:a:{vacc}"))
        await dp.feed_update(bot, cb_update(8, f"srv:cl:{vacc}"))
        assert "45.32.9.9" in s.texts()[-1] and "45.32.1.1" not in s.texts()[-1]
        await dp.feed_update(bot, cb_update(9, f"srv:cly:{vacc}"))
        await vultr.wait_pending(5)
        assert st["deleted"] == ["i-web", "i-left"] and "Удалено: <b>1</b> из 1" in s.texts()[-1]
    finally:
        await srv.close()


def test_live_servers():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_live_servers()
    print("OK")
