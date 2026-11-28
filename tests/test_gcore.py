"""Gcore Cloud against a fake API (shapes from the official gcore-python SDK): APIKey token, default project,
KVM regions, flavors with prices, reserved public IP first → server on it, allow-all security group, first-boot
script (no API password, or Gcore would skip user_data), tasks, delete with the reserved IP, IP quota → servers."""
import asyncio
import base64
import itertools

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import buyer
from bot.buyer import PurchaseJob, run_purchase
from bot.providers import PROVIDERS, RESERVE_PROVIDERS, gcore, make_client

ORIG_SLEEP = asyncio.sleep
BOT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBotBotBotBotBotBotBotBotBotBotBotBotBot vps-buyer-bot"
P = "/cloud"


def fake_gcore(ips, ip_quota=None):
    ips = itertools.cycle(ips)
    st = {"rips": {}, "deleted_rips": [], "instances": {}, "deleted": [], "created": [], "sg": [], "tasks": {},
          "delete_params": []}
    a = web.Application()

    @web.middleware
    async def auth(req, handler):
        if req.headers.get("Authorization") != "APIKey 7711_tok":
            return web.json_response({"message": "Invalid token"}, status=401)
        return await handler(req)
    a.middlewares.append(auth)

    def task(resources=None, error=None):
        tid = f"t{len(st['tasks'])}"
        st["tasks"][tid] = {"id": tid, "state": "ERROR" if error else "FINISHED", "error": error,
                            "created_resources": resources or {}}
        return web.json_response({"tasks": [tid]})
    a.router.add_get(P + "/v1/tasks/{tid}", lambda r: web.json_response(st["tasks"][r.match_info["tid"]]))
    a.router.add_get(P + "/v1/projects", lambda r: web.json_response({"count": 2, "results": [
        {"id": 5, "name": "other", "is_default": False}, {"id": 1288706, "name": "Default", "is_default": True}]}))
    a.router.add_get(P + "/v1/regions", lambda r: web.json_response({"count": 3, "results": [
        {"id": 128, "display_name": "Baku", "country": "az", "state": "ACTIVE", "access_level": "core", "has_kvm": True,
         "available_volume_types": ["ssd_hiiops", "standard"], "external_network_id": "ext-128"},
        {"id": 76, "display_name": "Luxembourg-2", "country": "LU", "state": "ACTIVE", "access_level": "edge",
         "has_kvm": True},
        {"id": 9, "display_name": "GPU only", "country": "NL", "state": "ACTIVE", "has_kvm": False}]}))

    async def flavors(req):
        assert req.query.get("include_prices") == "true"
        return web.json_response({"count": 3, "results": [
            {"flavor_id": "g1-standard-2-4", "flavor_name": "g1-standard-2-4", "vcpus": 2, "ram": 4096,
             "architecture": "x86_64", "disabled": False, "price_per_hour": 0.0301, "price_per_month": 21.97,
             "currency_code": "EUR"},
            {"flavor_id": "g1-standard-1-2", "flavor_name": "g1-standard-1-2", "vcpus": 1, "ram": 2048,
             "architecture": "x86_64", "disabled": False, "price_per_hour": 0.0151, "price_per_month": 10.99,
             "currency_code": "EUR"},
            {"flavor_id": "g1s-shared-1-0.5", "flavor_name": "g1s-shared-1-0.5", "vcpus": 1, "ram": 512,
             "architecture": "x86_64", "disabled": False, "price_per_hour": 0.004, "price_per_month": 3}]})
    a.router.add_get(P + "/v1/flavors/{p}/{r}", flavors)
    a.router.add_get(P + "/v1/images/{p}/{r}", lambda r: web.json_response({"results": [
        {"id": "img-u22", "name": "ubuntu-22.04-x64", "os_type": "linux", "status": "active"},
        {"id": "img-win", "name": "windows-2022", "os_type": "windows", "status": "active"}]}))
    a.router.add_get(P + "/v1/subnets/{p}/{r}/{sid}", lambda r: web.json_response(
        {"id": r.match_info["sid"], "cidr": {"sub-92.38": "92.38.140.0/22", "sub-5.188": "5.188.120.0/24"}.get(
            r.match_info["sid"], "1.0.0.0/24")}))
    a.router.add_get(P + "/v1/subnets/{p}/{r}", lambda r: web.json_response({"results": [] if st.get("no_subnets") else [
        {"network_id": "ext-128", "cidr": "92.38.140.0/24"}, {"network_id": "ext-128", "cidr": "0.0.0.0/0"},
        {"network_id": "private", "cidr": "10.0.0.0/24"}]}))

    async def rips(req):
        if req.method == "POST":
            b = await req.json()
            assert b == {"type": "external", "ip_family": "ipv4", "is_vip": False}
            if ip_quota is not None and len(st["rips"]) >= ip_quota:
                return task(error="Quota exceeded: external_ip_count limit 2")
            pid = f"port-{len(st['rips'])}"
            ip = next(ips)
            st["rips"][pid] = {"port_id": pid, "fixed_ip_address": ip, "is_external": True, "reservation": None,
                               "subnet_id": "sub-" + ".".join(ip.split(".")[:2])}
            return task({"ports": [pid]})
        return web.json_response({"results": [v for k, v in st["rips"].items() if k not in st["deleted_rips"]]})
    a.router.add_route("*", P + "/v1/reserved_fixed_ips/{p}/{r}", rips)

    async def rip(req):
        pid = req.match_info["pid"]
        if req.method == "DELETE":
            st["deleted_rips"].append(pid)
            return task()
        return web.json_response(st["rips"][pid])
    a.router.add_route("*", P + "/v1/reserved_fixed_ips/{p}/{r}/{pid}", rip)

    async def sgs(req):
        if req.method == "POST":
            b = (await req.json())["security_group"]
            assert {"direction": "ingress", "ethertype": "IPv4", "protocol": "any"} in b["security_group_rules"]
            st["sg"].append(b["name"])
            return web.json_response({"id": "sg-1", "name": b["name"]}, status=201)
        return web.json_response({"results": [{"id": "sg-1", "name": n} for n in st["sg"]]})
    a.router.add_route("*", P + "/v1/securitygroups/{p}/{r}", sgs)

    async def create(req):
        b = await req.json()
        st["created"].append(b)
        iid = f"inst-{len(st['instances'])}"
        iface = b["interfaces"][0]
        if iface["type"] == "reserved_fixed_ip":
            ip = st["rips"][iface["port_id"]]["fixed_ip_address"]
            st["rips"][iface["port_id"]]["reservation"] = {"resource_id": iid, "resource_type": "instance"}
        else:
            ip = "5.188.1.1"
        st["instances"][iid] = {"instance_id": iid, "instance_name": b["name"], "status": "BUILD", "polls": 0,
                                "ip": ip}
        return task({"instances": [iid]})
    a.router.add_post(P + "/v2/instances/{p}/{r}", create)

    def inst(iid):
        i = st["instances"][iid]
        i["polls"] += 1
        if i["polls"] > 1:
            i["status"] = "ACTIVE"
        return {"instance_id": iid, "instance_name": i["instance_name"], "status": i["status"], "region_id": 128,
                "fixed_ip_assignments": [{"external": True, "ip_address": i["ip"]},
                                         {"external": True, "ip_address": "2a03:90c0::1"}],
                "addresses": {"public": [{"addr": i["ip"], "type": "fixed"}]}}

    async def instance(req):
        iid = req.match_info["iid"]
        if iid not in st["instances"] or iid in st["deleted"]:
            return web.json_response({"message": "Instance not found"}, status=404)
        if req.method == "DELETE":
            st["deleted"].append(iid)
            st["delete_params"].append(dict(req.query))
            for pid in (req.query.get("reserved_fixed_ips") or "").split(","):
                if pid:
                    st["deleted_rips"].append(pid)
            return task()
        return web.json_response(inst(iid))
    a.router.add_route("*", P + "/v1/instances/{p}/{r}/{iid}", instance)
    a.router.add_get(P + "/v1/instances/{p}/{r}", lambda r: web.json_response({"results": [
        inst(i) for i in st["instances"] if i not in st["deleted"]] if r.match_info["r"] == "128" else []}))
    return a, st


async def scenario():
    assert "gcore" in PROVIDERS and "gcore" in RESERVE_PROVIDERS
    app, st = fake_gcore(["92.38.140.10", "92.38.141.20", "5.188.120.7"])
    srv = TestServer(app)
    await srv.start_server()
    url = str(srv.make_url(P))
    try:
        assert type(make_client("gcore", "", "7711_tok", "")).__name__ == "GcoreClient"
        async with gcore.GcoreClient("bad", url) as c:
            try:
                await c.check()
                raise AssertionError("bad token accepted")
            except gcore.ProviderError as e:
                assert e.status == 401 and "токен" in str(e)
        async with gcore.GcoreClient("APIKey 7711_tok", url) as c:
            assert await c.check() == "проект 1288706", "the default project"
            regs = {r.region: r for r in await c.region_statuses()}
            assert set(regs) == {"128", "76"}, "regions without VMs hidden"
            assert regs["128"].country == "AZ" and regs["128"].city == "Baku · Core"
            assert [p.prefix for p in await c.ip_pools("128")] == ["92.38.140"], "public subnet only"
            flavors = await c.list_flavors("128")
            assert [f.id for f in flavors] == ["g1-standard-1-2", "g1-standard-2-4"], "cheapest first, no shared"
            assert flavors[0].price == "10.99 EUR/мес · 0.0151/час" and flavors[0].ram_mb == 2048
            assert (await c.cheapest_plan("128"))[1] == 0.0151
            assert [i.id for i in await c.list_images("128")] == ["img-u22"]

            job = PurchaseJob("128", flavors[0], "img-u22", None, ["5.188.120"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3, bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert job.mode == "ports" and [k.ipv4 for k in job.kept] == [["5.188.120.7"]], job.log
            kept = job.kept[0]
            body = st["created"][0]
            good = next(k for k, v in st["rips"].items() if v["fixed_ip_address"] == "5.188.120.7")
            assert body["interfaces"] == [{"type": "reserved_fixed_ip", "port_id": good, "security_groups": [{"id": "sg-1"}]}]
            assert "password" not in body, "a password in the API would make Gcore skip the script"
            script = base64.b64decode(body["user_data"]).decode()
            assert BOT in script and "chpasswd" in script and "PasswordAuthentication yes" in script
            assert kept.admin_pass and body["volumes"][0]["type_name"] == "standard"
            assert set(st["deleted_rips"]) == set(st["rips"]) - {good}, "wrong IPs deleted"
            assert [s.id for s in await c.list_servers()] == [kept.id]
            await c.delete_server("128", kept.id)
            assert st["delete_params"][0]["reserved_fixed_ips"] == good and good in st["deleted_rips"]
    finally:
        await srv.close()

    # out of public IPs → servers with their own IP, not a stop
    app, st = fake_gcore(["92.38.140.10"], ip_quota=1)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with gcore.GcoreClient("7711_tok", str(srv.make_url(P))) as c:
            fl = (await c.list_flavors("128"))[0]
            job = PurchaseJob("128", fl, "img-u22", None, ["5.188.1"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        assert not job.error and job.mode == "servers" and [k.ipv4 for k in job.kept] == [["5.188.1.1"]], job.log
        assert st["created"][-1]["interfaces"][0]["type"] == "external"
    finally:
        await srv.close()


async def auto_scout_scenario():
    """A location with no known subnets: opening the IP step takes a few IPs (no servers), learns their whole
    subnets and shows the blocks right away."""
    import tempfile
    from aiogram import Bot, Dispatcher
    from bot import db
    from bot.config import config
    from bot.handlers import accounts, buy, menu, servers, update
    from tests.test_ip_select import buttons
    from tests.test_servers_update import UID, FakeSession, cb_update
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    app, st = fake_gcore(["92.38.141.5", "92.38.143.9", "5.188.120.7"])
    st["no_subnets"] = True
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "gcore_api_url", str(srv.make_url(P)))
    try:
        acc = await db.add_account(UID, "gcore", "p", "7711_tok", "")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        for r in (menu.router, accounts.router, servers.router, buy.router, update.router):
            r._parent_router = None
        dp.include_routers(menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "gcore", "account_id": acc, "regions": ["128"], "is_admin": True,
                            "region_labels": {"128": "🇦🇿 Baku · Core"}})
        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        assert any("подсети ещё неизвестны" in t for t in s.texts())
        text = s.texts()[-1]
        assert "92.38.140.#" in text and "92.38.143.#" in text and "5.188.120.#" in text, text
        assert "выпадал" in text and not st["created"], "IPs only, no servers"
        assert set(st["deleted_rips"]) == set(st["rips"]), "every IP released"
        assert "92.38.140.0/22" in await db.subnets("gcore", "128")
        assert len(await db.seen_ips("gcore", "128")) >= 3
    finally:
        await srv.close()


def test_gcore():
    orig = buyer.asyncio.sleep
    buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        asyncio.run(scenario())
        asyncio.run(auto_scout_scenario())
    finally:
        buyer.asyncio.sleep = orig


if __name__ == "__main__":
    test_gcore()
    print("OK")
