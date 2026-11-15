"""DigitalOcean: subnets from the geofeed are chosen by mask in the bot; droplets are bought until one
lands inside a chosen subnet. Fake API shaped like https://api.digitalocean.com/v2."""
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
from bot.providers import ProviderError, digitalocean  # noqa: E402
from bot.providers.digitalocean import DigitalOceanClient  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402

TOKEN = "do-token"
ORIG_SLEEP = asyncio.sleep
GEOFEED = """# DigitalOcean geofeed
5.101.96.0/21,NL,NL-NH,Amsterdam,1098 XH
142.93.96.0/20,DE,DE-HE,Frankfurt,60341
142.93.128.0/20,DE,DE-HE,Frankfurt,60341
164.92.160.0/20,DE,DE-HE,Frankfurt,60341
46.101.128.0/17,DE,DE-HE,Frankfurt,60341
168.144.228.0/22,None
2a03:b0c0:3::/48,DE,DE-HE,Frankfurt,60341
"""


def make_app(ips):
    ips = itertools.cycle(ips)
    st = {"droplets": {}, "deleted": [], "created": [], "limit": 3, "others": 0, "max_alive": 0, "limit_errors": 0}

    def alive():
        return st["others"] + len([d for d in st["droplets"] if d not in st["deleted"]])
    app = web.Application()
    app.router.add_get("/geo/google.csv", lambda r: web.Response(text=GEOFEED))

    @web.middleware
    async def auth(req, handler):
        if req.path.startswith("/v2") and req.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"id": "unauthorized", "message": "Unable to authenticate you"}, status=401)
        return await handler(req)
    app.middlewares.append(auth)
    app.router.add_get("/v2/account", lambda r: web.json_response(
        {"account": {"email": "me@example.com", "status": "active", "droplet_limit": st["limit"]}}))
    app.router.add_get("/v2/customers/my/balance", lambda r: web.json_response({"account_balance": "-25.50"}))
    app.router.add_get("/v2/regions", lambda r: web.json_response({"regions": [
        {"slug": "fra1", "name": "Frankfurt 1", "available": True, "sizes": ["s-1vcpu-1gb"]},
        {"slug": "ams3", "name": "Amsterdam 3", "available": True, "sizes": ["s-1vcpu-1gb"]},
        {"slug": "nyc2", "name": "New York 2", "available": False, "sizes": []}], "links": {}}))
    app.router.add_get("/v2/sizes", lambda r: web.json_response({"sizes": [
        {"slug": "s-2vcpu-2gb", "vcpus": 2, "memory": 2048, "disk": 60, "price_monthly": 18, "price_hourly": 0.02679,
         "available": True, "regions": ["fra1", "ams3"]},
        {"slug": "s-1vcpu-1gb", "vcpus": 1, "memory": 1024, "disk": 25, "price_monthly": 6, "price_hourly": 0.00893,
         "available": True, "regions": ["fra1", "ams3"]},
        {"slug": "c-32", "vcpus": 32, "memory": 65536, "disk": 400, "price_monthly": 1000, "price_hourly": 1.5,
         "available": True, "regions": ["nyc3"]}], "links": {}}))
    app.router.add_get("/v2/images", lambda r: web.json_response({"images": [
        {"slug": "ubuntu-24-04-x64", "distribution": "Ubuntu", "name": "24.04 (LTS) x64", "regions": ["fra1", "ams3"],
         "status": "available"},
        {"slug": "debian-12-x64", "distribution": "Debian", "name": "12 x64", "regions": ["fra1"], "status": "available"}],
        "links": {}}))

    def droplet(did):
        d = st["droplets"][did]
        d["polls"] += 1
        ready = d["polls"] > 1
        v4 = [{"ip_address": d["ip"], "netmask": d["mask"], "gateway": "x", "type": "public"},
              {"ip_address": "10.110.0.2", "netmask": "255.255.240.0", "type": "private"}] if ready else []
        return {"id": did, "name": d["name"], "status": "active" if ready else "new", "networks": {"v4": v4}}

    async def droplets(req):
        if req.method == "GET":
            return web.json_response({"droplets": [], "meta": {"total": alive()}})
        if alive() >= st["limit"]:
            st["limit_errors"] += 1
            return web.json_response({"id": "unprocessable_entity",
                                      "message": "creating this/these droplet(s) will exceed your droplet limit"}, status=422)
        b = await req.json()
        st["created"].append(b)
        did = 9000 + len(st["droplets"])
        ip, mask = next(ips)
        st["droplets"][did] = {"name": b["name"], "polls": 0, "ip": ip, "mask": mask}
        st["max_alive"] = max(st["max_alive"], alive())
        return web.json_response({"droplet": {"id": did, "name": b["name"], "status": "new", "networks": {"v4": []}}},
                                 status=202)

    async def one(req):
        did = int(req.match_info["id"])
        if req.method == "DELETE":
            st["deleted"].append(did)
            return web.Response(status=204)
        return web.json_response({"droplet": droplet(did)})
    app.router.add_route("*", "/v2/droplets", droplets)
    app.router.add_route("*", "/v2/droplets/{id}", one)
    return app, st


def text_update(n, text):
    m = Message(message_id=n, date=datetime.now(), chat=Chat(id=UID, type="private"),
                from_user=User(id=UID, is_bot=False, first_name="A"), text=text)
    return Update(update_id=n, message=m)


def markup_texts(s):
    last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
    return [b.text for row in last.reply_markup.inline_keyboard for b in row]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    digitalocean.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    digitalocean._GEOFEED = None
    app, st = make_app([("164.92.170.5", "255.255.240.0"), ("142.93.100.9", "255.255.240.0"),
                        ("46.101.130.1", "255.255.128.0"), ("142.93.140.2", "255.255.240.0")])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "do_api_url", str(srv.make_url("/v2")))
    object.__setattr__(config, "do_geofeed_url", str(srv.make_url("/geo/google.csv")))
    try:
        async with DigitalOceanClient("bad", config.do_api_url) as c:
            try:
                await c.check(); raise AssertionError("bad token accepted")
            except ProviderError as e:
                assert e.status == 401 and "неверный токен" in str(e)
        async with DigitalOceanClient(TOKEN, config.do_api_url) as c:
            assert await c.check() == "me@example.com, баланс $25.50"
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["fra1"].available, regs["fra1"].country, regs["fra1"].city) == (True, "DE", "Франкфурт")
            assert not regs["nyc2"].available
            pools = await c.ip_pools("fra1")
            assert [p.prefix for p in pools] == ["46.101.128.0/17", "142.93.96.0/20", "142.93.128.0/20",
                                                 "164.92.160.0/20"], "only Frankfurt IPv4 subnets"
            flavors = await c.list_flavors("fra1")
            assert [f.id for f in flavors] == ["s-1vcpu-1gb", "s-2vcpu-2gb"]
            assert flavors[0].price == "$6/мес · $0.009/час"
            imgs = await c.list_images("fra1")
            assert [i.id for i in imgs] == ["ubuntu-24-04-x64", "debian-12-x64"]

        # ---------- the bot: choose subnets by mask ----------
        acc = await db.add_account(UID, "digitalocean", "me@example.com", TOKEN, "")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "digitalocean", "account_id": acc, "regions": ["fra1"],
                            "region_labels": {"fra1": "🇩🇪 Франкфурт · fra1"}})
        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        groups = s.texts()[-1]
        print(groups, "\n")
        assert "подсетей: <b>4</b>" in groups and "<code>142.93.#.#</code> — 2 подсети" in groups and "— 1 подсеть" in groups
        assert "142.93.#.# · 2" in markup_texts(s)
        await dp.feed_update(bot, cb_update(2, "buy:ipg:142.93"))
        sub = s.texts()[-1]
        print(sub, "\n")
        assert "<code>142.93.96.0/20</code> · 16 бл." in sub and "164.92" not in sub
        await dp.feed_update(bot, cb_update(3, "buy:ipt:1"))         # tick 142.93.96.0/20
        assert "✅ 142.93.96.0/20" in markup_texts(s)
        await dp.feed_update(bot, cb_update(4, "buy:ipg:-"))         # back to groups
        assert "✅ 142.93.#.# · 2" in markup_texts(s)
        await dp.feed_update(bot, cb_update(5, "buy:ip:custom"))      # own subnet with a mask
        await dp.feed_update(bot, text_update(6, "46.101.128.0/18"))
        assert "<code>46.101.128.0/18</code>" in s.texts()[-1] and "Выбрано: <b>2</b>" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(7, "buy:ipok"))
        data = await ctx.get_data()
        assert data["prefixes"] == ["142.93.96.0/20", "46.101.128.0/18"], data["prefixes"]
        await dp.feed_update(bot, cb_update(8, "buy:cnt:2"))
        await dp.feed_update(bot, cb_update(9, "buy:fl:0"))
        await dp.feed_update(bot, cb_update(10, "buy:img:0"))
        assert "Как входить на сервер" in s.texts()[-1]
        await dp.feed_update(bot, cb_update(110, "buy:auth:pw"))
        confirm = s.texts()[-1]
        assert "Ищем IP: <code>142.93.96.0/20</code>, <code>46.101.128.0/18</code>" in confirm, confirm
        assert "Быстрый режим" not in confirm
        data = await ctx.get_data()

        # ---------- buying: an IP counts only when it is inside a chosen subnet ----------
        async with DigitalOceanClient(TOKEN, config.do_api_url) as c:
            job = PurchaseJob("fra1", buy.Flavor(**data["flavor"]), data["image"]["id"], None, data["prefixes"], 2,
                              max_attempts=10, parallel=1, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert [k.ipv4[0] for k in job.kept] == ["142.93.100.9", "46.101.130.1"]
        assert st["deleted"] == [9000], "164.92.170.5 is outside the chosen subnets"
        assert all(k.admin_pass and len(k.admin_pass) == 16 for k in job.kept)
        b = st["created"][0]
        assert (b["region"], b["size"], b["image"]) == ("fra1", "s-1vcpu-1gb", "ubuntu-24-04-x64")
        assert "chpasswd --encrypted" in b["user_data"] and "chage -d" in b["user_data"]
        assert job.subnets[:2] == ["164.92.160.0/20", "142.93.96.0/20"], job.subnets

        # ---------- no attempt limit; batches never exceed the account's droplet limit ----------
        app2, st2 = make_app([("1.2.3.4", "255.255.255.0")] * 7 + [("142.93.99.1", "255.255.240.0")])
        srv2 = TestServer(app2)
        await srv2.start_server()
        try:
            async with DigitalOceanClient(TOKEN, str(srv2.make_url("/v2"))) as c:
                job = PurchaseJob("fra1", buy.Flavor(**data["flavor"]), "ubuntu-24-04-x64", None, ["142.93.96.0/20"],
                                  1, max_attempts=0, parallel=3, active_timeout=30, boot_volume_gb=10)
                await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
                assert [k.ipv4[0] for k in job.kept] == ["142.93.99.1"] and not job.error, job.log
                assert job.attempts >= 8, "the old 50-attempt cap is gone and 8 droplets were needed"
                assert st2["max_alive"] <= 3, "never more droplets at once than the account allows"
                assert st2["limit_errors"] == 0, "batches are sized to the free slots"
                # account already full with other droplets → a clear message, nothing created
                st2["others"], before = 2, len(st2["created"])  # + the droplet bought above = 3
                job2 = PurchaseJob("fra1", buy.Flavor(**data["flavor"]), "ubuntu-24-04-x64", None, ["142.93.96.0/20"],
                                   1, max_attempts=0, parallel=3, active_timeout=30, boot_volume_gb=10)
                await run_purchase(c, job2, lambda j: ORIG_SLEEP(0))
                assert job2.error and "уже 3 из 3" in job2.error and len(st2["created"]) == before
        finally:
            await srv2.close()

        # ---------- ⭐ subnets this datacenter really handed out come first ----------
        await db.add_seen_ips("digitalocean", "fra1", ["164.92.170.5", "164.92.171.9", "107.170.11.229"])
        await db.add_subnets("digitalocean", "fra1", ["107.170.0.0/17"])
        await ctx.set_data({"provider": "digitalocean", "account_id": acc, "regions": ["fra1"],
                            "region_labels": {"fra1": "🇩🇪 Франкфурт · fra1"}})
        await dp.feed_update(bot, cb_update(20, "buy:reg:0"))
        groups = s.texts()[-1]
        print("\n" + groups)
        lines = [l for l in groups.splitlines() if "<code>" in l]
        assert lines[0].startswith("⭐ <code>164.92.#.#</code>") and "выпадало 2" in lines[0], lines
        assert "⭐ <code>107.170.#.#</code>" in groups, "subnet seen here but missing from the geofeed is added"
        assert "⭐ Выбрать выпадавшие" in markup_texts(s)
        await dp.feed_update(bot, cb_update(21, "buy:iphits"))
        picked = [p["prefix"] for i, p in enumerate((await ctx.get_data())["pools"])
                  if i in (await ctx.get_data())["selected"]]
        assert picked == ["107.170.0.0/17", "164.92.160.0/20"], picked
    finally:
        await srv.close()
        digitalocean.asyncio.sleep = ORIG_SLEEP


def test_digitalocean():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_digitalocean()
    print("OK")
