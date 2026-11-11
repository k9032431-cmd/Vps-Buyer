"""Serverspace against a fake API shaped like https://docs.serverspace.ru/docs/ru/v1/openapi.json.
Key point: IP masks reveal whole subnets, so the IP step lists every /24 block of them."""
import asyncio
import itertools
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.buyer import PurchaseJob, run_purchase  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import ProviderError, serverspace  # noqa: E402
from bot.providers.serverspace import ServerspaceClient  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402

KEY = "ss-key"
ORIG_SLEEP = asyncio.sleep


def make_app(ips):
    """ips: (ip, mask) handed out to new servers, in order."""
    ips = itertools.cycle(ips)
    st = {"servers": {}, "deleted": [], "created": [], "tasks": {}}
    app = web.Application()

    @web.middleware
    async def auth(req, handler):
        if req.headers.get("X-API-KEY") != KEY:
            return web.Response(status=401)
        return await handler(req)
    app.middlewares.append(auth)
    app.router.add_get("/api/v1/project", lambda r: web.json_response(
        {"project": {"id": 1, "balance": 812.4, "currency": "RUB", "state": "Active"}}))
    app.router.add_get("/api/v1/locations", lambda r: web.json_response({"locations": [
        {"id": "am2", "system_volume_min": 10240, "bandwidth_min": 50, "cpu_quantity_options": [1, 2, 4],
         "ram_size_options": [1024, 2048, 4096], "allow_creating_servers": True, "allow_public_network": True},
        {"id": "nj3", "allow_creating_servers": False, "allow_public_network": True},
        {"id": "ds1", "system_volume_min": 10240, "bandwidth_min": 50, "cpu_quantity_options": [1],
         "ram_size_options": [1024], "allow_creating_servers": True, "allow_public_network": False}]}))
    app.router.add_get("/api/v1/images", lambda r: web.json_response({"images": [
        {"id": "Ubuntu-22.04-X64", "location_id": "am2", "type": "Ubuntu", "os_version": "22.04", "architecture": "X64"},
        {"id": "Debian-12-X64", "location_id": "am2", "type": "Debian", "os_version": "12", "architecture": "X64"},
        {"id": "Win-2022", "location_id": "am2", "type": "Windows", "os_version": "2022", "architecture": "X64"},
        {"id": "Ubuntu-22.04-X64-nj", "location_id": "nj3", "type": "Ubuntu", "os_version": "22.04"}]}))

    async def price(req):
        b = await req.json()
        return web.json_response({"price": b["cpu"] * 300 + b["ram_mb"] / 1024 * 150})
    app.router.add_post("/api/v1/servers/price", price)

    def server_json(sid):
        s = st["servers"][sid]
        s["polls"] += 1
        ready = s["polls"] > 1
        nics = [{"id": 1, "network_type": "PublicShared", "ip_address": s["ip"], "mask": s["mask"],
                 "gateway": "x", "bandwidth_mbps": 50}] if ready else []
        return {"id": sid, "location_id": s["loc"], "name": s["name"], "state": "Active" if ready else "New",
                "is_power_on": ready, "nics": nics, "login": "root", "password": f"pw-{sid}"}

    async def servers(req):
        if req.method == "POST":
            b = await req.json()
            st["created"].append(b)
            sid = f"l{len(st['servers']) + 1}"
            ip, mask = next(ips)
            st["servers"][sid] = {"name": b["name"], "loc": b["location_id"], "polls": 0, "ip": ip, "mask": mask}
            tid = f"t{sid}"
            st["tasks"][tid] = sid
            return web.json_response({"task_id": tid}, status=201)
        existing = [{"id": "old", "location_id": "am2", "state": "Active", "nics": [
            {"network_type": "PublicShared", "ip_address": "95.182.81.10", "mask": 22}]}]
        return web.json_response({"servers": existing})

    async def server(req):
        sid = req.match_info["id"]
        if req.method == "DELETE":
            st["deleted"].append(sid)
            return web.json_response({"task_id": "d" + sid})
        return web.json_response({"server": server_json(sid)})

    async def task(req):
        tid = req.match_info["id"]
        return web.json_response({"task": {"id": tid, "server_id": st["tasks"].get(tid), "is_completed": "InProgress"}})
    app.router.add_route("*", "/api/v1/servers", servers)
    app.router.add_route("*", "/api/v1/servers/{id}", server)
    app.router.add_get("/api/v1/tasks/{id}", task)
    return app, st


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    serverspace.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    app, st = make_app([("31.44.2.7", 24), ("185.22.152.40", 23), ("31.44.2.8", 24)])
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url("")).rstrip("/")
    object.__setattr__(config, "serverspace_api_url", base)
    try:
        async with ServerspaceClient("bad", base) as c:
            try:
                await c.check(); raise AssertionError("bad key accepted")
            except ProviderError as e:
                assert e.status == 401 and "неверный API-ключ" in str(e)
        async with ServerspaceClient(KEY, base) as c:
            assert await c.check() == "баланс 812.4 ₽"
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["am2"].available, regs["am2"].country, regs["am2"].city) == (True, "NL", "Амстердам")
            assert (regs["nj3"].available, regs["nj3"].reason) == (False, "создание серверов закрыто")
            assert (regs["ds1"].available, regs["ds1"].reason) == (False, "нет публичных IP")
            # one existing IP with a /22 mask → all four /24 blocks of the subnet
            assert [p.prefix for p in regs["am2"].pools] == ["95.182.80", "95.182.81", "95.182.82", "95.182.83"]
            assert regs["am2"].pools[0].cidrs == ["95.182.80.0/22"]
            flavors = await c.list_flavors("am2")
            assert [f.id for f in flavors] == ["1-1024-10240-50", "1-2048-10240-50", "2-2048-10240-50",
                                               "2-4096-10240-50"], [f.id for f in flavors]
            assert flavors[0].price == "450 ₽/мес"
            cheapest, hour = await c.cheapest_plan("am2")
            assert cheapest.id == "1-1024-10240-50" and hour == 0.62
            imgs = await c.list_images("am2")
            assert [i.name for i in imgs] == ["Ubuntu 22.04", "Debian 12"], "Windows and other locations hidden"
            job = PurchaseJob("am2", flavors[0], "Ubuntu-22.04-X64", None, ["31.44.2"], 2, max_attempts=10,
                              parallel=1, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            print("\n".join(job.log))
            assert [s.ipv4[0] for s in job.kept] == ["31.44.2.7", "31.44.2.8"]
            assert [s.admin_pass for s in job.kept] == ["pw-l1", "pw-l3"], "password from the API"
            assert st["deleted"] == ["l2"]
            assert job.subnets == ["31.44.2.0/24", "185.22.152.0/23"], job.subnets
            body = st["created"][0]
            assert body["volumes"] == [{"name": "boot", "size_mb": 10240}] and body["networks"] == [{"bandwidth_mbps": 50}]
            assert (body["cpu"], body["ram_mb"], body["location_id"]) == (1, 1024, "am2")
        await db.add_subnets("serverspace", "am2", job.subnets)

        # --- bot: the IP step shows every block of every known subnet ---
        acc = await db.add_account(UID, "serverspace", "ss", KEY, "")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "serverspace", "account_id": acc, "regions": ["am2"],
                            "region_labels": {"am2": "🇳🇱 Амстердам · am2"}})
        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        text = s.texts()[-1]
        print("\n" + text)
        for block in ("95.182.80", "95.182.83", "31.44.2", "185.22.152", "185.22.153"):
            assert f"<code>{block}.#</code>" in text, block
        last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
        assert "🔍 Разведать доступные IP" in [b.text for row in last.reply_markup.inline_keyboard for b in row]
    finally:
        await srv.close()
        serverspace.asyncio.sleep = ORIG_SLEEP


def test_serverspace():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_serverspace()
    print("OK")
