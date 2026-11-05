"""Timeweb Cloud and Aeza clients against fake APIs shaped like their official specs."""
import asyncio
import itertools
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.buyer import PurchaseJob, run_purchase  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import ProviderError, aeza, timeweb  # noqa: E402
from bot.providers.aeza import AezaClient  # noqa: E402
from bot.providers.timeweb import TimewebClient  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402

TW_TOKEN, AEZA_KEY = "tw-token", "aeza-key"


# ---------------- Timeweb ----------------
def timeweb_app(ips, confirm_delete=False):
    ips = itertools.cycle(ips)
    st = {"servers": {}, "deleted": [], "created": []}
    app = web.Application()

    @web.middleware
    async def auth(req, handler):
        if req.headers.get("Authorization") != f"Bearer {TW_TOKEN}":
            return web.json_response({"status_code": 401, "message": "Unauthorized"}, status=401)
        return await handler(req)
    app.middlewares.append(auth)
    app.router.add_get("/api/v1/account/finances", lambda r: web.json_response(
        {"finances": {"balance": 1500.5, "currency": "RUB"}}))
    app.router.add_get("/api/v2/locations", lambda r: web.json_response({"locations": [
        {"location": "ru-1", "location_code": "spb", "availability_zones": ["spb-3"]},
        {"location": "nl-1", "location_code": "ams", "availability_zones": ["ams-1"]},
        {"location": "kz-1", "location_code": "ala", "availability_zones": ["ala-1"]}]}))
    app.router.add_get("/api/v1/presets/servers", lambda r: web.json_response({"server_presets": [
        {"id": 2447, "location": "ru-1", "price": 477, "cpu": 1, "ram": 2048, "disk": 30720,
         "description_short": "1 x 3.3 ГГц • 2 ГБ • 30 ГБ"},
        {"id": 4795, "location": "ru-1", "price": 1100, "cpu": 2, "ram": 4096, "disk": 51200,
         "description_short": "2 x 3.3 ГГц • 4 ГБ • 50 ГБ"},
        {"id": 2453, "location": "nl-1", "price": 650, "cpu": 1, "ram": 1024, "disk": 15360,
         "description_short": "1 x 3.3 ГГц • 1 ГБ • 15 ГБ"}]}))
    app.router.add_get("/api/v1/os/servers", lambda r: web.json_response({"servers_os": [
        {"id": 79, "family": "linux", "name": "ubuntu", "version": "22.04"},
        {"id": 99, "family": "linux", "name": "ubuntu", "version": "24.04"},
        {"id": 47, "family": "linux", "name": "debian", "version": "12"}]}))

    def server_json(sid):
        s = st["servers"][sid]
        s["polls"] += 1
        ready = s["polls"] > 1
        nets = [{"type": "public", "ips": [{"type": "ipv4", "ip": s["ip"], "is_main": True, "ptr": ""},
                                           {"type": "ipv6", "ip": "2a03::1", "is_main": False, "ptr": ""}]}] \
            if ready else []
        return {"id": sid, "name": s["name"], "status": "on" if ready else "installing", "networks": nets,
                "availability_zone": s["zone"], "root_pass": "tw-root-" + str(sid)}

    async def servers(req):
        if req.method == "POST":
            body = await req.json()
            st["created"].append(body)
            sid = 1000 + len(st["servers"])
            st["servers"][sid] = {"name": body["name"], "zone": body["availability_zone"], "polls": 0, "ip": next(ips)}
            return web.json_response({"server": {"id": sid, "name": body["name"], "status": "installing",
                                                 "networks": [], "root_pass": None}}, status=201)
        return web.json_response({"servers": [{"id": 1, "availability_zone": "spb-3", "networks": [
            {"type": "public", "ips": [{"type": "ipv4", "ip": "92.53.90.7"}]}]}]})

    async def server(req):
        sid = int(req.match_info["id"])
        if req.method == "DELETE":
            if confirm_delete:
                return web.json_response({"server_delete": {"hash": "abc", "is_moved_in_quarantine": False}})
            st["deleted"].append(sid)
            return web.Response(status=204)
        return web.json_response({"server": server_json(sid)})
    app.router.add_route("*", "/api/v1/servers", servers)
    app.router.add_route("*", "/api/v1/servers/{id}", server)
    return app, st


async def timeweb_scenario():
    timeweb.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    app, st = timeweb_app(["5.23.48.10", "92.53.90.21", "5.23.48.11", "92.53.90.22"])
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url("")).rstrip("/")
    try:
        async with TimewebClient("bad", base) as c:
            try:
                await c.check(); raise AssertionError("bad token accepted")
            except ProviderError as e:
                assert e.status == 401 and "неверный токен" in str(e)
        async with TimewebClient(TW_TOKEN, base) as c:
            assert await c.check() == "баланс 1500.5 ₽"
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["spb-3"].available, regs["spb-3"].country, regs["spb-3"].city) == (True, "RU", "Санкт-Петербург")
            assert regs["ams-1"].city == "Амстердам" and not regs["ala-1"].available
            assert regs["spb-3"].pools[0].prefix == "92.53.90"
            flavors = await c.list_flavors("spb-3")
            assert [f.id for f in flavors] == ["2447", "4795"] and flavors[0].price == "477 ₽/мес"
            assert (flavors[0].vcpus, flavors[0].ram_mb, flavors[0].disk_gb) == (1, 2048, 30)
            cheapest, hour = await c.cheapest_plan("spb-3")
            assert cheapest.id == "2447" and hour == 0.66
            imgs = await c.list_images("spb-3")
            assert "ubuntu 22.04" in [i.name for i in imgs]
            job = PurchaseJob("spb-3", flavors[0], "79", None, ["92.53.90"], 2, max_attempts=10, parallel=1,
                              active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            print("\n".join(job.log))
            assert [s.ipv4[0] for s in job.kept] == ["92.53.90.21", "92.53.90.22"]
            assert [s.admin_pass for s in job.kept] == ["tw-root-1001", "tw-root-1003"], "root_pass from Timeweb"
            assert st["deleted"] == [1000, 1002]
            body = st["created"][0]
            assert body == {"name": body["name"], "preset_id": 2447, "os_id": 79, "availability_zone": "spb-3",
                            "is_ddos_guard": False}
    finally:
        await srv.close()

    # deletion confirmation enabled in the account → a clear message, not a silent failure
    app, st = timeweb_app(["1.1.1.1"], confirm_delete=True)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with TimewebClient(TW_TOKEN, str(srv.make_url("")).rstrip("/")) as c:
            try:
                await c.delete_server("spb-3", "5"); raise AssertionError("no error")
            except ProviderError as e:
                assert "Подтверждение удаления" in str(e)
    finally:
        await srv.close()


# ---------------- Aeza ----------------
def aeza_app(ips):
    ips = itertools.cycle(ips)
    st = {"services": {}, "deleted": [], "orders": [], "passwords": {}}
    app = web.Application()

    def group(label, disabled=False):
        return {"id": 2, "payload": {"label": label, "mode": "shared", "isDisabled": disabled}}
    products = [
        {"id": 153, "type": "vps", "name": "MSKs-1", "isPrivate": False, "group": group("RU-SHARED"),
         "prices": {"hour": 4, "month": 593}, "summaryConfiguration": {"cpu": {"base": 1}, "ram": {"base": 2}, "rom": {"base": 30}}},
        {"id": 154, "type": "vps", "name": "MSKs-2", "isPrivate": False, "group": group("RU-SHARED"),
         "prices": {"hour": 7, "month": 1187}, "summaryConfiguration": {"cpu": {"base": 2}, "ram": {"base": 4}, "rom": {"base": 60}}},
        {"id": 456, "type": "vps", "name": "Custom MSK-s", "isPrivate": False, "group": group("RU-SHARED"),
         "prices": {"month": 450}, "summaryConfiguration": {}},
        {"id": 375, "type": "vps", "name": "DEs-1", "isPrivate": False, "group": group("DE-SHARED"),
         "prices": {"hour": 4, "month": 593}, "summaryConfiguration": {"cpu": {"base": 1}, "ram": {"base": 2}, "rom": {"base": 30}}},
        {"id": 264, "type": "vps", "name": "SPBs-1", "isPrivate": False, "group": group("SPB-SHARED", disabled=True),
         "prices": {"hour": 4, "month": 593}, "summaryConfiguration": {"cpu": {"base": 1}, "ram": {"base": 2}, "rom": {"base": 30}}},
        {"id": 900, "type": "domain", "name": "domain", "group": group("X")},
    ]
    app.router.add_get("/api/services/products", lambda r: web.json_response({"data": {"items": products, "total": 6}}))
    app.router.add_get("/api/v2/services/products", lambda r: web.json_response(
        {"items": [{"id": 153, "isAvailable": True}, {"id": 154, "isAvailable": False},
                   {"id": 375, "isAvailable": True}, {"id": 264, "isAvailable": True}], "total": 4}))

    @web.middleware
    async def auth(req, handler):
        if "/products" not in req.path and req.headers.get("X-API-Key") != AEZA_KEY:
            return web.json_response({"error": {"slug": "not_auth", "message": "Not authenticated"}}, status=401)
        return await handler(req)
    app.middlewares.append(auth)
    app.router.add_get("/api/desktop", lambda r: web.json_response({"data": {"balance": {"value": 1234}}}))
    app.router.add_get("/api/os", lambda r: web.json_response({"data": {"items": [
        {"id": 940, "name": "Ubuntu 22.04", "group": "Ubuntu"}, {"id": 941, "name": "Debian 12"}]}}))

    async def services(req):
        items = [{"id": 1, "productId": 153, "ip": "77.110.104.5", "status": "active", "name": "old"}]
        return web.json_response({"data": {"items": items, "total": 1}})
    app.router.add_get("/api/services", services)

    async def orders(req):
        body = await req.json()
        st["orders"].append(body)
        sid = 5000 + len(st["services"])
        st["services"][sid] = {"name": body["name"], "polls": 0, "ip": next(ips)}
        return web.json_response({"data": {"createdServiceIds": [sid]}})
    app.router.add_post("/api/services/orders", orders)

    async def service(req):
        sid = int(req.match_info["id"])
        if req.method == "DELETE":
            st["deleted"].append(sid)
            return web.json_response({"data": {}})
        s = st["services"][sid]
        s["polls"] += 1
        ready = s["polls"] > 1
        item = {"id": sid, "name": s["name"], "status": "active" if ready else "installing",
                "ip": s["ip"] if ready else None}
        return web.json_response({"data": {"items": [item], "total": 1}})
    app.router.add_route("*", "/api/services/{id}", service)

    async def change_password(req):
        st["passwords"][int(req.match_info["id"])] = (await req.json())["password"]
        return web.json_response({"data": {}})
    app.router.add_post("/api/services/{id}/changePassword", change_password)
    return app, st


async def aeza_scenario():
    aeza.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    app, st = aeza_app(["77.110.104.30", "185.177.1.4", "77.110.104.31"])
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url("/api"))
    try:
        async with AezaClient("bad", base) as c:
            try:
                await c.check(); raise AssertionError("bad key accepted")
            except ProviderError as e:
                assert e.status == 401 and "неверный API-ключ" in str(e)
        async with AezaClient(AEZA_KEY, base) as c:
            assert await c.check() == "баланс €12.34"
            regs = {r.region: r for r in await c.region_statuses()}
            assert set(regs) == {"ru", "de", "spb"}
            assert (regs["ru"].available, regs["ru"].country, regs["ru"].city) == (True, "RU", "Москва")
            assert (regs["spb"].available, regs["spb"].reason) == (False, "нет в наличии")
            assert regs["ru"].pools[0].prefix == "77.110.104"
            flavors = await c.list_flavors("ru")
            assert [f.name for f in flavors] == ["MSKs-1"], "unavailable (v2) and custom tariffs are hidden"
            assert flavors[0].price == "€0.04/час · €5.93/мес" and (flavors[0].ram_mb, flavors[0].disk_gb) == (2048, 30)
            cheapest, hour = await c.cheapest_plan("ru")
            assert cheapest.name == "MSKs-1" and hour == 0.04
            imgs = await c.list_images("ru")
            assert [i.id for i in imgs] == ["940", "941"]
            job = PurchaseJob("ru", flavors[0], "940", None, ["77.110.104"], 2, max_attempts=10, parallel=1,
                              active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            print("\n".join(job.log))
            assert [s.ipv4[0] for s in job.kept] == ["77.110.104.30", "77.110.104.31"]
            assert st["deleted"] == [5001]
            assert [s.admin_pass for s in job.kept] == [st["passwords"][5000], st["passwords"][5002]]
            order = st["orders"][0]
            assert (order["productId"], order["term"], order["count"], order["method"], order["parameters"]["os"],
                    order["autoProlong"]) == (153, "hour", 1, "balance", 940, False)
    finally:
        await srv.close()


# ---------------- bot: connect by token, provider list ----------------
async def bot_scenario():
    app, _ = timeweb_app(["1.1.1.1"])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "timeweb_api_url", str(srv.make_url("")).rstrip("/"))
    try:
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
        await dp.feed_update(bot, cb_update(1, "buy"))
        last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
        names = [b.text for row in last.reply_markup.inline_keyboard for b in row]
        assert {"☁️ Timeweb Cloud", "☁️ Aeza"} <= set(names), names
        await dp.feed_update(bot, cb_update(2, "acc:add:timeweb"))
        assert "Подтверждение удаления сервисов" in s.texts()[-1]
        user = User(id=UID, is_bot=False, first_name="A")
        msg = Message(message_id=7, date=datetime.now(), chat=Chat(id=UID, type="private"), from_user=user, text=TW_TOKEN)
        await dp.feed_update(bot, Update(update_id=3, message=msg))
        assert "Аккаунт подключён" in s.texts()[-1] and "Timeweb Cloud" in s.texts()[-1], s.texts()[-1]
        acc = (await db.list_accounts(UID, "timeweb"))[0]
        assert acc.password == TW_TOKEN
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "timeweb"})
        await dp.feed_update(bot, cb_update(4, f"buy:acc:{acc.id}"))
        text = s.texts()[-1]
        last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
        assert "Доступно: <b>2</b> из 3" in text and "🇷🇺 Санкт-Петербург · spb-3" in str(last.reply_markup), text
    finally:
        await srv.close()


ORIG_SLEEP = asyncio.sleep


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    try:
        await timeweb_scenario()
        await aeza_scenario()
    finally:
        timeweb.asyncio.sleep = ORIG_SLEEP
    await bot_scenario()


def test_timeweb_aeza():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_timeweb_aeza()
    print("OK")
