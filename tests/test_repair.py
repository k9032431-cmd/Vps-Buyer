"""'🛠 Restore access' for HostVDS servers bought without the allow_all group / password user_data."""
import asyncio
import base64
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.handlers import accounts, buy, menu, servers, update  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402


def make_app(calls):
    app = web.Application()

    async def tokens(req):
        base = str(req.url.origin())
        cat = [{"type": t, "endpoints": [{"interface": "public", "region_id": "eu-west2", "url": f"{base}/{t}"}]}
               for t in ("compute", "network")]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}}, status=201,
                                 headers={"X-Subject-Token": "t"})
    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_get("/network/v2.0/security-groups", lambda r: web.json_response(
        {"security_groups": [{"id": "sg-allow", "name": "allow_all"}]}))
    app.router.add_get("/compute/servers/{id}", lambda r: web.json_response({"server": {
        "id": r.match_info["id"], "image": {"id": "img-ubuntu"}, "metadata": {"network_plan": "unlim-shared-100"},
        "security_groups": [{"name": "default"}]}}))

    async def action(req):
        calls.append((await req.json(), req.headers.get("OpenStack-API-Version")))
        return web.Response(status=202)
    app.router.add_post("/compute/servers/{id}/action", action)
    return app


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    calls = []
    srv = TestServer(make_app(calls))
    await srv.start_server()
    object.__setattr__(config, "hostvds_auth_url", str(srv.make_url("/identity")))
    try:
        acc = await db.add_account(UID, "hostvds", "u", "p", "u")
        await db.add_server(UID, acc, "eu-west2", "srv-87", "vps-a", "95.182.81.87", "old-nova-pass")
        row = (await db.list_servers(UID))[0]
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)

        await dp.feed_update(bot, cb_update(1, f"srv:{row.id}"))
        card = s.texts()[-1]
        assert "<code>ssh root@95.182.81.87</code>" in card and "Восстановить доступ" in card
        await dp.feed_update(bot, cb_update(2, f"srv:fix:{row.id}"))
        assert "IP останется прежним" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(3, f"srv:fixyes:{row.id}"))
        done = s.texts()[-1]
        print(done)
        assert done.startswith("✅ <b>Готово!</b>")

        (add_sg, _), (rebuild, version) = calls
        assert add_sg == {"addSecurityGroup": {"name": "sg-allow"}}
        assert version == "compute 2.57", "user_data in rebuild needs microversion 2.57"
        r = rebuild["rebuild"]
        assert r["imageRef"] == "img-ubuntu" and r["metadata"] == {"network_plan": "unlim-shared-100"}
        assert "chpasswd --encrypted" in base64.b64decode(r["user_data"]).decode()
        new_pw = (await db.get_server(UID, row.id)).admin_pass
        assert new_pw != "old-nova-pass" and f"<code>{new_pw}</code>" in done
    finally:
        await srv.close()


def test_repair():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_repair()
    print("OK")
