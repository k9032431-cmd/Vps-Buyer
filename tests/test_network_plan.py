"""HostVDS site API (regions / plans / network plans) + network plan step + server metadata.
Response shapes copied from https://hostvds.com/api/regions/ and /api/plans/?region=… (Nov 2026)."""
import asyncio
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db
from bot.buyer import PurchaseJob, run_purchase
from bot.config import config
from bot.handlers import accounts, buy, menu, servers, update
from bot.providers import Flavor, hostvds_public
from bot.providers.hostvds import HostVDSClient
from tests.test_servers_update import UID, FakeSession, cb_update

NET = "87d32954-aac8-436f-ae47-6ed032d58ff7"
REGIONS = [
    {"region": "eu-west2", "az": "par1", "city_code": "paris", "country_code": "fr", "is_available": True,
     "is_maintenance": False, "is_read_only": False, "is_out_of_stock": False, "available_ips_v4": 494,
     "networks": [{"network_id": "v6", "network_name": "Internet-IPv6-only", "available_ips_v4": 0},
                  {"network_id": NET, "network_name": "Internet-22", "available_ips_v4": 5},
                  {"network_id": "full-net", "network_name": "Internet-19", "available_ips_v4": 0}]},
    {"region": "eu-north2", "city_code": "Riga-1", "country_code": "lv", "is_available": True,
     "is_maintenance": False, "is_out_of_stock": False, "available_ips_v4": 435, "networks": []},
    {"region": "us-east", "city_code": "dallas", "country_code": "us", "is_available": True,
     "is_maintenance": False, "is_out_of_stock": True, "available_ips_v4": 255, "networks": []},
    {"region": "eu-west1", "city_code": "amsterdam", "country_code": "nl", "is_available": False,
     "is_maintenance": False, "is_out_of_stock": False, "available_ips_v4": 1201, "networks": []},
]


def net_opts(default):
    return {
        "unlim-50": {"base_traffic": 1000000, "max_bandwidth": 50, "monthly_price": 3},
        default: {"visible": True, "base_traffic": 500, "max_bandwidth": 50, "monthly_price": 0},
        "unlim-shared-100": {"visible": True, "base_traffic": 1000000, "max_bandwidth": 100, "monthly_price": 0.49},
        "unlim-shared-1000": {"visible": True, "base_traffic": 1000000, "max_bandwidth": 1000, "monthly_price": 0.99},
    }


PLANS = [
    {"uuid": "u1", "name": "HostVDS-1", "kind": "standard", "flavor_uuid": "flv-1", "vcpus": 1, "ram": 1024,
     "disk": 10240, "monthly": 1.49, "currency": "eur", "is_out_of_stock": False, "force_out_of_stock": False,
     "extra_opts": {"network_opts": net_opts("default-50"), "default_network_opt": "default-50"}},
    {"uuid": "u2", "name": "highload-24", "kind": "highload", "flavor_uuid": "flv-24", "vcpus": 24, "ram": 98304,
     "disk": 491520, "monthly": 134.7, "currency": "eur", "is_out_of_stock": True,
     "extra_opts": {"network_opts": net_opts("default-10000"), "default_network_opt": "default-10000"}},
]


def make_app(created):
    app = web.Application()
    app.router.add_get("/api/regions/", lambda r: web.json_response(REGIONS))
    app.router.add_get("/api/plans/", lambda r: web.json_response(PLANS if r.query["region"] == "eu-west2" else []))

    async def tokens(req):
        base = str(req.url.origin())
        cat = [{"type": t, "endpoints": [{"interface": "public", "region_id": r, "url": f"{base}/{t}"}
                                         for r in ("eu-west2", "eu-north2", "us-east", "eu-west1", "asia-x")]}
               for t in ("compute", "network", "image")]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}}, status=201,
                                 headers={"X-Subject-Token": "t"})
    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_get("/network/v2.0/networks", lambda r: web.json_response({"networks": [
        {"id": NET, "shared": True}, {"id": "full-net", "shared": True}]}))
    app.router.add_get("/network/v2.0/subnets", lambda r: web.json_response({"subnets": [
        {"id": "s22", "network_id": NET, "ip_version": 4, "cidr": "95.182.87.0/24"},
        {"id": "s19", "network_id": "full-net", "ip_version": 4, "cidr": "45.10.20.0/24"}]}))
    app.router.add_get("/image/v2/images", lambda r: web.json_response({"images": [
        {"id": "img-u22", "name": "Ubuntu 22.04 LTS"}]}))

    sg = {"groups": [], "rules": []}

    async def list_sg(req):
        return web.json_response({"security_groups": [g for g in sg["groups"] if g["name"] == req.query.get("name")]})

    async def create_sg(req):
        g = dict((await req.json())["security_group"], id="sg-new")
        sg["groups"].append(g)
        return web.json_response({"security_group": g}, status=201)

    async def create_rule(req):
        sg["rules"].append((await req.json())["security_group_rule"])
        return web.json_response({"security_group_rule": {"id": "r"}}, status=201)
    app.router.add_get("/network/v2.0/security-groups", list_sg)
    app.router.add_post("/network/v2.0/security-groups", create_sg)
    app.router.add_post("/network/v2.0/security-group-rules", create_rule)
    app["sg"] = sg

    async def create(req):
        body = (await req.json())["server"]
        created.append(body)
        return web.json_response({"server": {"id": "srv1", "adminPass": "pw"}}, status=202)
    app.router.add_post("/compute/servers", create)
    app.router.add_get("/compute/servers/{id}", lambda r: web.json_response({"server": {
        "id": "srv1", "name": "x", "status": "ACTIVE", "addresses": {"Internet-22": [{"addr": "95.182.87.40", "version": 4}]}}}))
    return app


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    created = []
    srv = TestServer(make_app(created))
    await srv.start_server()
    object.__setattr__(config, "hostvds_public_api", str(srv.make_url("/api")))
    object.__setattr__(config, "hostvds_auth_url", str(srv.make_url("/identity")))
    hostvds_public._CACHE.clear()
    try:
        assert hostvds_public.city_name("Riga-2") == "Рига 2" and hostvds_public.city_name("hel") == "Хельсинки"
        async with HostVDSClient(config.hostvds_auth_url, "u", "p", "u") as c:
            st = {s.region: s for s in await c.region_statuses()}
            assert st["eu-west2"].available and (st["eu-west2"].country, st["eu-west2"].city) == ("FR", "Париж")
            assert st["eu-north2"].city == "Рига 1"
            assert (st["us-east"].available, st["us-east"].reason) == (False, "нет в наличии")
            assert (st["eu-west1"].available, st["eu-west1"].reason) == (False, "создание серверов закрыто")
            assert not st["asia-x"].available
            flavors = await c.list_flavors("eu-west2")
            assert [f.name for f in flavors] == ["HostVDS-1"], "out-of-stock tariffs are hidden"
            f = flavors[0]
            assert (f.id, f.price, f.default_network_plan) == ("flv-1", "€1.49/мес", "default-50")
            assert [p["name"] for p in f.network_plans] == ["default-50", "unlim-shared-100", "unlim-shared-1000",
                                                            "unlim-50"], f.network_plans
            pools = await c.ip_pools("eu-west2")
            assert {p.prefix: p.free for p in pools} == {"45.10.20": 0, "95.182.87": 5}

        # --- bot screens ---
        acc = await db.add_account(UID, "hostvds", "u", "p", "u")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "hostvds"})
        await dp.feed_update(bot, cb_update(1, f"buy:acc:{acc}"))
        text = s.texts()[-1]
        assert "Доступно: <b>2</b> из 5" in text and "нет в наличии" in text, text
        paris = (await ctx.get_data())["regions"].index("eu-west2")
        assert "🇫🇷 Париж · eu-west2" in (await ctx.get_data())["region_labels"].values()
        await dp.feed_update(bot, cb_update(2, f"buy:reg:{paris}"))
        text = s.texts()[-1]
        assert "<code>95.182.87.#</code> · свободно 5" in text and "45.10.20" not in text, text
        assert "Скрыто блоков без свободных IP: 1" in text
        await dp.feed_update(bot, cb_update(3, "buy:ipt:0"))
        await dp.feed_update(bot, cb_update(4, "buy:ipok"))
        await dp.feed_update(bot, cb_update(5, "buy:cnt:1"))
        await dp.feed_update(bot, cb_update(6, "buy:fl:0"))
        plans_markup = [b.text for row in s.calls[-1].reply_markup.inline_keyboard for b in row] \
            if getattr(s.calls[-1], "reply_markup", None) else []
        print("NETWORK PLAN BUTTONS:", plans_markup)
        assert plans_markup[:3] == ["⭐ Limited 50 Mbps · бесплатно", "Unlimited Shared 100 Mbps · €0.49/мес",
                                    "Unlimited Shared 1 Gbps · €0.99/мес"], plans_markup
        assert "🔽 Показать все планы (4)" in plans_markup
        await dp.feed_update(bot, cb_update(7, "buy:npall"))
        await dp.feed_update(bot, cb_update(8, "buy:np:2"))         # Unlimited Shared 1 Gbps
        await dp.feed_update(bot, cb_update(9, "buy:img:0"))
        confirm = s.texts()[-1]
        print(confirm)
        assert "📶 Network plan: <b>Unlimited Shared 1 Gbps</b> · €0.99/мес" in confirm
        data = await ctx.get_data()

        # --- the server is created exactly like the HostVDS panel does it ---
        job = PurchaseJob(data["region"], Flavor(**data["flavor"]), data["image"]["id"], None, data["prefixes"], 1,
                          max_attempts=3, parallel=1, active_timeout=10, boot_volume_gb=10,
                          metadata={"network_plan": data["network_plan"]["name"], "os_distro": data["image"]["distro"],
                                    "kind": data["flavor"]["kind"]})
        async with HostVDSClient(config.hostvds_auth_url, "u", "p", "u") as c:
            await run_purchase(c, job, lambda j: asyncio.sleep(0))
        body = created[-1]
        assert body["flavorRef"] == "flv-1" and body["imageRef"] == "img-u22"
        assert body["metadata"] == {"network_plan": "unlim-shared-1000", "os_distro": "ubuntu", "kind": "standard"}
        # account had no allow_all group → the bot created it with inbound rules, like the panel's default
        sg = srv.app["sg"]
        assert [g["name"] for g in sg["groups"]] == ["allow_all"]
        assert sorted(r["ethertype"] for r in sg["rules"]) == ["IPv4", "IPv6"]
        assert all(r["direction"] == "ingress" for r in sg["rules"])
        assert body["security_groups"] == [{"name": "sg-new"}] and body["user_data"]
        assert job.kept and job.kept[0].ipv4[0] == "95.182.87.40"
    finally:
        await srv.close()


def test_network_plan():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_network_plan()
    print("OK")
