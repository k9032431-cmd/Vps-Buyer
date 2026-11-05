"""Selectel VDS client + bot flow against a fake api.vscale.io (response shapes from the official docs)."""
import asyncio
import itertools
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db
from bot.buyer import PurchaseJob, run_purchase
from bot.config import config

# keep tests offline: the HostVDS site API is unreachable → OpenStack fallback is used
object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")
from bot.handlers import accounts, buy, menu, servers, update
from bot.providers import selectel
from bot.providers.selectel import SelectelVDSClient
from tests.test_servers_update import UID, FakeSession, cb_update
from aiogram.types import Chat, Message, Update, User
from datetime import datetime

TOKEN = "tok123"


def make_app(ips):
    ips = itertools.cycle(ips)
    state = {"scalets": {}, "deleted": [], "created": [], "rate_limited": False}
    app = web.Application()

    @web.middleware
    async def auth(req, handler):
        if req.headers.get("X-Token") != TOKEN:
            return web.Response(status=403, headers={"Vscale-Error-Message": "forbidden"})
        return await handler(req)
    app.middlewares.append(auth)

    async def account(req):
        return web.json_response({"info": {"email": "me@example.com", "name": "Me"}})

    async def balance(req):
        return web.json_response({"balance": 0, "bonus": 0, "summ": 123450, "status": "ok"})

    async def locations(req):
        return web.json_response([
            {"id": "spb0", "active": True, "description": "Цветочная 21", "rplans": ["small"], "templates": []},
            {"id": "msk0", "active": True, "description": "Москва", "rplans": ["small"], "templates": []},
            {"id": "spb1", "active": False, "description": "old", "rplans": [], "templates": []},
        ])

    async def rplans(req):
        return web.json_response([{"id": "small", "cpus": 1, "memory": 512, "disk": 20480, "addresses": 1,
                                   "locations": ["spb0", "msk0"]}])

    async def prices(req):
        return web.json_response({"default": {"backup": 1, "small": {"hour": 29, "month": 20000}}})

    async def images(req):
        return web.json_response([
            {"id": "ubuntu_22.04_64_001_master", "description": "Ubuntu 22.04", "active": True,
             "locations": ["spb0", "msk0"], "rplans": ["small"]},
            {"id": "debian_old", "description": "Debian old", "active": True, "locations": ["msk0"], "rplans": ["small"]},
        ])

    async def scalets(req):
        if req.method == "POST":
            if not state["rate_limited"]:
                state["rate_limited"] = True
                return web.Response(status=429, headers={"Vscale-Error-Message": "too_many_requests"})
            body = await req.json()
            assert body["password"] and body["do_start"] is True and body["location"] == "spb0", body
            ctid = 1000 + len(state["scalets"])
            state["scalets"][ctid] = {"ctid": ctid, "name": body["name"], "location": body["location"],
                                      "status": "defined", "public_address": {}, "polls": 0, "ip": next(ips)}
            state["created"].append(body)
            return web.json_response({k: v for k, v in state["scalets"][ctid].items() if k not in ("polls", "ip")})
        return web.json_response([{"ctid": 1, "location": "spb0", "status": "started",
                                   "public_address": {"address": "31.184.1.1"}}])

    async def scalet(req):
        ctid = int(req.match_info["id"])
        if req.method == "DELETE":
            state["deleted"].append(ctid)
            return web.json_response({"ctid": ctid, "status": "deleted"})
        s = state["scalets"][ctid]
        s["polls"] += 1
        started = s["polls"] > 1
        out = {"ctid": ctid, "name": s["name"], "status": "started" if started else "defined",
               "public_address": {"address": s["ip"], "netmask": "255.255.255.0"} if started else {}}
        return web.json_response([out])  # the docs show GET /scalets/:ctid wrapped in a list

    for path, h in (("/v1/account", account), ("/v1/billing/balance", balance), ("/v1/locations", locations),
                    ("/v1/rplans", rplans), ("/v1/billing/prices", prices), ("/v1/images", images)):
        app.router.add_get(path, h)
    app.router.add_route("*", "/v1/scalets", scalets)
    app.router.add_route("*", "/v1/scalets/{id}", scalet)
    return app, state


async def scenario():
    tmp = tempfile.mkdtemp()
    object.__setattr__(config, "db_path", f"{tmp}/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    orig_sleep = asyncio.sleep
    selectel.asyncio.sleep = lambda s: orig_sleep(0)
    selectel.CREATE_INTERVAL = 0
    app, st = make_app(["5.188.1.10", "31.184.2.20", "5.188.1.11", "31.184.2.21"])
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url("/v1"))
    object.__setattr__(config, "selectel_api_url", base)
    try:
        # --- client ---
        async with SelectelVDSClient("bad", base) as c:
            try:
                await c.check(); raise AssertionError("bad token accepted")
            except selectel.ProviderError as e:
                assert e.status == 403 and "неверный токен" in str(e)
        async with SelectelVDSClient(TOKEN, base) as c:
            assert await c.check() == "me@example.com, баланс 1234.50 ₽"
            regs = {r.region: r for r in await c.region_statuses()}
            assert regs["spb0"].available and regs["spb0"].city == "Санкт-Петербург" and regs["spb0"].country == "RU"
            assert regs["spb0"].pools[0].prefix == "31.184.1"
            assert not regs["spb1"].available and regs["spb1"].reason == "локация закрыта"
            flavors = await c.list_flavors("spb0")
            assert flavors[0].price == "200 ₽/мес" and flavors[0].disk_gb == 20, flavors
            imgs = await c.list_images("spb0", flavors[0])
            assert [i.id for i in imgs] == ["ubuntu_22.04_64_001_master"]
            job = PurchaseJob("spb0", flavors[0], imgs[0].id, None, ["31"], 2, max_attempts=10, parallel=2,
                              active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: orig_sleep(0))
            print("\n".join(job.log))
            assert sorted(s.ipv4[0] for s in job.kept) == ["31.184.2.20", "31.184.2.21"], job.log
            assert all(s.admin_pass for s in job.kept)
            assert len(st["deleted"]) == 2 and job.seen

        # --- bot flow: add account by token, then see locations with flags ---
        session = FakeSession()
        bot = Bot("1:x", session=session)
        dp = Dispatcher()
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
        await dp.feed_update(bot, cb_update(1, "acc:add:selectel"))
        assert "API" in session.texts()[-1]
        user = User(id=UID, is_bot=False, first_name="A")
        msg = Message(message_id=5, date=datetime.now(), chat=Chat(id=UID, type="private"), from_user=user, text=TOKEN)
        await dp.feed_update(bot, Update(update_id=2, message=msg))
        assert "Selectel VDS: <code>me@example.com</code> подключён" in session.texts()[-1], session.texts()[-1]
        acc = (await db.list_accounts(UID, "selectel"))[0]
        assert acc.password == TOKEN
        await dp.feed_update(bot, cb_update(3, "buy:prov:selectel"))
        await dp.feed_update(bot, cb_update(4, f"buy:acc:{acc.id}"))
        text = session.texts()[-1]
        print(text)
        assert "Доступно: 2 из 3" in text and "🇷🇺 Санкт-Петербург · spb0" in str(session.calls[-1].reply_markup)
    finally:
        await srv.close()
        selectel.asyncio.sleep = orig_sleep


def test_selectel():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_selectel()
    print("OK")
