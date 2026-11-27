"""Hetzner, Vultr, Linode, Civo against fake APIs (shapes from the Hetzner OpenAPI spec, vultr/govultr,
Linode and Civo docs) and fake geofeeds."""
import asyncio
import base64
import itertools
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.buyer import PurchaseJob, run_purchase  # noqa: E402
from bot.providers import PROVIDER_NAMES, PROVIDERS, Flavor, civo, hetzner, linode, make_client, vultr  # noqa: E402
from bot.providers.geofeed import _CACHE  # noqa: E402
from bot.scanner import ScanJob, run_scan  # noqa: E402

ORIG_SLEEP = asyncio.sleep
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID6LbRUzalQL49DX5sqqfgDxxf0Ce0EetDiMpoHpXkuC me@laptop"
BOT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBotBotBotBotBotBotBotBotBotBotBotBotBot vps-buyer-bot"
GEO = """# fake geofeed
116.202.0.0/16,DE,DE-SN,Falkenstein,
49.12.0.0/16,DE,DE-BY,Nuremberg,
95.216.0.0/16,FI,FI-18,Helsinki,
45.32.0.0/21,US,US-NJ,Piscataway,
45.76.0.0/21,DE,DE-HE,Frankfurt,
172.104.0.0/18,US,US-NJ,Cedar Knolls,
139.162.0.0/17,DE,DE-HE,Frankfurt,
2a01:4f8::/32,DE,DE-SN,Falkenstein,
"""


def ok(data, status=200):
    return web.json_response(data, status=status)


# ---------------- Hetzner ----------------
def hetzner_app(ips):
    ips = itertools.cycle(ips)
    st = {"primary": {}, "deleted_ips": [], "servers": {}, "deleted": [], "created": [], "keys": []}
    a = web.Application()
    a.router.add_get("/geo.csv", lambda r: web.Response(text=GEO))
    a.router.add_get("/v1/locations", lambda r: ok({"locations": [
        {"name": "fsn1", "city": "Falkenstein", "country": "DE"}, {"name": "hel1", "city": "Helsinki", "country": "FI"},
        {"name": "ash", "city": "Ashburn, VA", "country": "US"}]}))
    price = lambda loc: {"location": loc, "price_hourly": {"gross": "0.0063"}, "price_monthly": {"gross": "3.9500"}}
    a.router.add_get("/v1/server_types", lambda r: ok({"server_types": [
        {"name": "cx22", "cores": 2, "memory": 4.0, "disk": 40, "deprecated": False, "prices": [price("fsn1"), price("hel1")]},
        {"name": "cax11", "cores": 2, "memory": 4.0, "disk": 40, "deprecated": False, "prices": [price("fsn1")]}],
        "meta": {"pagination": {"next_page": None}}}))
    a.router.add_get("/v1/images", lambda r: ok({"images": [
        {"id": 67794396, "name": "ubuntu-24.04", "description": "Ubuntu 24.04", "architecture": "x86"},
        {"id": 103908130, "name": "ubuntu-24.04", "description": "Ubuntu 24.04", "architecture": "arm"}],
        "meta": {"pagination": {"next_page": None}}}))

    async def primary(req):
        if req.method == "POST":
            b = await req.json()
            assert (b["type"], b["assignee_type"], b["auto_delete"]) == ("ipv4", "server", True), b
            pid = 500 + len(st["primary"])
            st["primary"][pid] = next(ips)
            return ok({"primary_ip": {"id": pid, "ip": st["primary"][pid]}}, 201)
    a.router.add_post("/v1/primary_ips", primary)

    async def del_primary(req):
        st["deleted_ips"].append(int(req.match_info["id"]))
        return web.Response(status=204)
    a.router.add_delete("/v1/primary_ips/{id}", del_primary)

    async def keys(req):
        if req.method == "POST":
            b = await req.json()
            st["keys"].append({"id": 70 + len(st["keys"]), **b})
            return ok({"ssh_key": st["keys"][-1]}, 201)
        return ok({"ssh_keys": st["keys"], "meta": {"pagination": {"next_page": None}}})
    a.router.add_route("*", "/v1/ssh_keys", keys)

    async def servers(req):
        b = await req.json()
        st["created"].append(b)
        sid = 9000 + len(st["servers"])
        st["servers"][sid] = st["primary"].get(b["public_net"].get("ipv4"), "1.1.1.1")
        return ok({"server": {"id": sid, "name": b["name"], "status": "initializing",
                              "public_net": {"ipv4": {"ip": st["servers"][sid]}}}, "root_password": None}, 201)
    a.router.add_post("/v1/servers", servers)

    async def server(req):
        sid = int(req.match_info["id"])
        if req.method == "DELETE":
            st["deleted"].append(sid)
            return ok({"action": {}})
        return ok({"server": {"id": sid, "name": "x", "status": "running",
                              "public_net": {"ipv4": {"ip": st["servers"][sid]}}}})
    a.router.add_route("*", "/v1/servers/{id}", server)
    return a, st


async def hetzner_scenario():
    app, st = hetzner_app(["5.75.1.1", "116.202.9.9", "88.99.1.1", "116.202.10.10"])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "hetzner_geofeed_url", str(srv.make_url("/geo.csv")))
    try:
        async with hetzner.HetznerClient("t", str(srv.make_url("/v1"))) as c:
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["fsn1"].available, regs["fsn1"].city, regs["fsn1"].country) == (True, "Фалькенштайн", "DE")
            assert not regs["ash"].available, "no server type is sold there"
            assert [p.prefix for p in await c.ip_pools("fsn1")] == ["116.202.0.0/16"]
            fl = await c.list_flavors("fsn1")
            assert fl[0].id == "cx22" and fl[0].price == "€3.95/мес · €0.0063/час" and fl[0].ram_mb == 4096
            assert [i.id for i in await c.list_images("fsn1", fl[0])] == ["67794396"], "x86 image for cx"
            job = PurchaseJob("fsn1", fl[0], "67794396", None, ["116.202.0.0/16"], 2, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert job.mode == "ports" and [k.ipv4[0] for k in job.kept] == ["116.202.9.9", "116.202.10.10"]
        assert set(st["deleted_ips"]) == set(st["primary"]) - {501, 503}, "every unused Primary IP is released"
        assert len(st["created"]) == 2 and [b["public_net"]["ipv4"] for b in st["created"]] == [501, 503]
        b = st["created"][0]
        assert "chpasswd" in b["user_data"] and job.kept[0].admin_pass, "password set by us on first boot"
        # scouting = Primary IPs only
        async with hetzner.HetznerClient("t", str(srv.make_url("/v1"))) as c:
            scan = ScanJob("fsn1", Flavor("-", "-", 0, 0, 0), "-", 4)
            await run_scan(c, scan, lambda j: ORIG_SLEEP(0))
        assert scan.created == 4 and scan.deleted == 4 and len(st["created"]) == 2, "no servers for scouting"
        # SSH key path
        async with hetzner.HetznerClient("t", str(srv.make_url("/v1"))) as c:
            s1 = await c.create_server("fsn1", "k", fl[0], "67794396", None, 10, ssh_key=PUB, extra_keys=[BOT])
        b = st["created"][-1]
        assert b["ssh_keys"] == [70, 71] and "user_data" not in b and s1.admin_pass is None
    finally:
        await srv.close()


# ---------------- Vultr ----------------
def vultr_app(ips):
    ips = itertools.cycle(ips)
    st = {"rips": {}, "deleted_rips": [], "instances": {}, "deleted": [], "created": [], "busy": {}}
    a = web.Application()
    a.router.add_get("/geo", lambda r: web.Response(text=GEO))
    a.router.add_get("/v2/account", lambda r: ok({"account": {"email": "me@x.io", "balance": -12.5, "pending_charges": 2.5}}))
    a.router.add_get("/v2/regions", lambda r: ok({"regions": [{"id": "ewr", "city": "New Jersey", "country": "US"},
                                                              {"id": "fra", "city": "Frankfurt", "country": "DE"}],
                                                 "meta": {"links": {"next": ""}}}))
    a.router.add_get("/v2/plans", lambda r: ok({"plans": [
        {"id": "vc2-1c-1gb", "vcpu_count": 1, "ram": 1024, "disk": 25, "monthly_cost": 5, "locations": ["ewr", "fra"]}],
        "meta": {"links": {"next": ""}}}))
    a.router.add_get("/v2/regions/ewr/availability", lambda r: ok({"available_plans": ["vc2-1c-1gb"]}))
    a.router.add_get("/v2/regions/fra/availability", lambda r: ok({"available_plans": ["vc2-2c-4gb"]}))
    a.router.add_get("/v2/os", lambda r: ok({"os": [{"id": 2284, "name": "Ubuntu 24.04 LTS x64", "arch": "x64", "family": "ubuntu"},
                                                    {"id": 501, "name": "Windows 2022", "arch": "x64", "family": "windows"}],
                                             "meta": {"links": {"next": ""}}}))

    async def rips(req):
        if req.method == "POST":
            b = await req.json()
            assert (b["region"], b["ip_type"]) == ("ewr", "v4")
            rid = f"rip-{len(st['rips'])}"
            st["rips"][rid] = {"id": rid, "subnet": next(ips), "instance_id": ""}
            return ok({"reserved_ip": st["rips"][rid]}, 201)
        return ok({"reserved_ips": [r for r in st["rips"].values() if r["id"] not in st["deleted_rips"]],
                   "meta": {"links": {"next": ""}}})
    a.router.add_route("*", "/v2/reserved-ips", rips)

    async def del_rip(req):
        rid = req.match_info["id"]
        if st["rips"][rid]["instance_id"] and st["busy"].get(rid, 0) < 2:  # instance still being destroyed
            st["busy"][rid] = st["busy"].get(rid, 0) + 1
            return ok({"error": "The server must be closed to detach this IP because it is being used as the "
                                "main IP.", "status": 400}, 400)
        st["deleted_rips"].append(rid)
        return web.Response(status=204)
    a.router.add_delete("/v2/reserved-ips/{id}", del_rip)
    a.router.add_route("*", "/v2/ssh-keys", lambda r: ok({"ssh_keys": [], "meta": {"links": {"next": ""}}})
                       if r.method == "GET" else ok({"ssh_key": {"id": "key-1"}}, 201))

    async def instances(req):
        b = await req.json()
        st["created"].append(b)
        iid = f"i-{len(st['instances'])}"
        rip = st["rips"][b["reserved_ipv4"]]
        rip["instance_id"] = iid
        st["instances"][iid] = rip["subnet"]
        return ok({"instance": {"id": iid, "label": b["label"], "main_ip": "0.0.0.0", "status": "pending",
                                "power_status": "stopped", "default_password": "V#ultr-pass"}}, 202)
    a.router.add_post("/v2/instances", instances)

    async def instance(req):
        iid = req.match_info["id"]
        if req.method == "DELETE":
            st["deleted"].append(iid)
            return web.Response(status=204)
        return ok({"instance": {"id": iid, "label": "x", "main_ip": st["instances"][iid], "status": "active",
                                "power_status": "running", "default_password": "V#ultr-pass"}})
    a.router.add_route("*", "/v2/instances/{id}", instance)
    return a, st


async def vultr_scenario():
    app, st = vultr_app(["149.28.1.1", "45.32.3.3"])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "vultr_geofeed_url", str(srv.make_url("/geo")))
    try:
        async with vultr.VultrClient("t", str(srv.make_url("/v2"))) as c:
            assert await c.check() == "me@x.io, баланс $10.00"
            assert [p.prefix for p in await c.ip_pools("ewr")] == ["45.32.0.0/21"], "Piscataway = New Jersey"
            assert [i.id for i in await c.list_images("ewr")] == ["2284"]
            fl = (await c.list_flavors("ewr"))[0]
            assert await c.list_flavors("fra") == [], "a plan sold out in the location is not offered"
            job = PurchaseJob("ewr", fl, "2284", None, ["45.32.0.0/21"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert [k.ipv4[0] for k in job.kept] == ["45.32.3.3"] and job.kept[0].admin_pass == "V#ultr-pass"
            assert st["created"][0]["reserved_ipv4"] == "rip-1"
            assert set(st["deleted_rips"]) == set(st["rips"]) - {"rip-1"}, "unused reserved IPs released"
            # deleting the instance also frees its reserved IP (it would keep being billed); Vultr refuses
            # while the instance is still being destroyed — the bot keeps trying in the background
            await c.delete_server("ewr", job.kept[0].id)
            assert st["deleted"] == ["i-0"]
        await vultr.wait_pending(5)
        assert st["deleted_rips"][-1] == "rip-1" and st["busy"]["rip-1"] == 2
    finally:
        await srv.close()


# ---------------- Linode ----------------
def linode_app(ips):
    ips = itertools.cycle(ips)
    st = {"created": [], "deleted": [], "inst": {}}
    a = web.Application()
    a.router.add_get("/geo", lambda r: web.Response(text=GEO))
    a.router.add_get("/v4/regions", lambda r: ok({"data": [
        {"id": "us-east", "label": "Newark, NJ", "country": "us", "status": "ok", "capabilities": ["Linodes"], "site_type": "core"},
        {"id": "eu-central", "label": "Frankfurt, DE", "country": "de", "status": "outage", "capabilities": ["Linodes"]}],
        "page": 1, "pages": 1}))
    a.router.add_get("/v4/linode/types", lambda r: ok({"data": [
        {"id": "g6-nanode-1", "label": "Nanode 1GB", "class": "nanode", "vcpus": 1, "memory": 1024, "disk": 25600,
         "price": {"hourly": 0.0075, "monthly": 5.0}, "region_prices": [{"id": "us-east", "hourly": 0.009, "monthly": 6.0}]}],
        "page": 1, "pages": 1}))

    async def create(req):
        b = await req.json()
        st["created"].append(b)
        lid = 70 + len(st["inst"])
        st["inst"][lid] = next(ips)
        return ok({"id": lid, "label": b["label"], "status": "provisioning", "ipv4": [st["inst"][lid]]})
    a.router.add_post("/v4/linode/instances", create)

    async def one(req):
        lid = int(req.match_info["id"])
        if req.method == "DELETE":
            st["deleted"].append(lid)
            return ok({})
        return ok({"id": lid, "label": "x", "status": "running", "ipv4": [st["inst"][lid], "192.168.1.5"]})
    a.router.add_route("*", "/v4/linode/instances/{id}", one)
    return a, st


async def linode_scenario():
    app, st = linode_app(["50.116.1.1", "172.104.5.5"])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "linode_geofeed_url", str(srv.make_url("/geo")))
    try:
        async with linode.LinodeClient("t", str(srv.make_url("/v4"))) as c:
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["us-east"].city, regs["us-east"].available) == ("Newark", True) and not regs["eu-central"].available
            assert [p.prefix for p in await c.ip_pools("us-east")] == ["172.104.0.0/18"], "Cedar Knolls = Newark"
            fl = (await c.list_flavors("us-east"))[0]
            assert fl.price == "$6/мес · $0.009/час" and fl.disk_gb == 25, "region price wins"
            job = PurchaseJob("us-east", fl, "linode/ubuntu24.04", None, ["172.104.0.0/18"], 1, max_attempts=0,
                              parallel=1, active_timeout=10, boot_volume_gb=10, bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        assert [k.ipv4 for k in job.kept] == [["172.104.5.5"]] and st["deleted"] == [70], "private IPs ignored"
        b = st["created"][0]
        assert len(b["root_pass"]) == 24 and b["authorized_keys"] == [BOT] and job.kept[0].admin_pass == st["created"][1]["root_pass"]
    finally:
        await srv.close()


# ---------------- Civo ----------------
def civo_app():
    st = {"created": [], "deleted": [], "regions_seen": []}
    a = web.Application()

    @web.middleware
    async def auth(req, handler):
        if req.headers.get("Authorization") != "bearer t":
            return ok({"code": "authentication_failed", "reason": "bad key"}, 401)
        return await handler(req)
    a.middlewares.append(auth)
    a.router.add_get("/v2/quota", lambda r: ok({"instance_count_usage": 1, "instance_count_limit": 10}))
    a.router.add_get("/v2/regions", lambda r: ok([
        {"code": "LON1", "name": "London 1", "country": "uk", "out_of_capacity": False, "features": {"iaas": True}},
        {"code": "FRA1", "name": "Frankfurt 1", "country": "de", "out_of_capacity": True, "features": {"iaas": True}}]))
    a.router.add_get("/v2/sizes", lambda r: ok([
        {"name": "g3.xsmall", "nice_name": "Extra Small", "cpu_cores": 1, "ram_mb": 1024, "disk_gb": 25, "selectable": True},
        {"name": "g4s.kube.small", "cpu_cores": 1, "ram_mb": 2048, "disk_gb": 40, "selectable": True}]))
    a.router.add_get("/v2/disk_images", lambda r: ok([{"id": "img-1", "name": "ubuntu-jammy", "version": "22.04",
                                                      "distribution": "ubuntu", "state": "available"}]))
    a.router.add_get("/v2/networks", lambda r: ok([{"id": "net-x", "default": False}, {"id": "net-def", "default": True}]))
    a.router.add_route("*", "/v2/sshkeys", lambda r: ok([]) if r.method == "GET" else ok({"result": "success", "id": "key-9"}))

    async def instances(req):
        if req.method == "GET":
            return ok({"items": [{"id": "old", "public_ip": "74.220.1.9"}]})
        b = await req.json()
        st["created"].append(b)
        return ok({"id": "inst-1", "hostname": b["hostname"], "status": "BUILDING", "public_ip": None})
    a.router.add_route("*", "/v2/instances", instances)

    async def one(req):
        st["regions_seen"].append(req.query.get("region"))
        if req.method == "DELETE":
            st["deleted"].append(req.match_info["id"])
            return ok({"result": "success"})
        return ok({"id": "inst-1", "hostname": "x", "status": "ACTIVE", "public_ip": "74.220.2.2",
                   "initial_password": "civo-pass"})
    a.router.add_route("*", "/v2/instances/{id}", one)
    return a, st


async def civo_scenario():
    app, st = civo_app()
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with civo.CivoClient("t", str(srv.make_url("/v2"))) as c:
            assert await c.check() == "квота инстансов: 1/10"
            regs = {r.region: r for r in await c.region_statuses()}
            assert (regs["LON1"].available, regs["LON1"].city) == (True, "Лондон") and not regs["FRA1"].available
            assert regs["LON1"].pools[0].prefix == "74.220.1"
            assert [f.id for f in await c.list_flavors("LON1")] == ["g3.xsmall"], "kube sizes hidden"
            fl = (await c.list_flavors("LON1"))[0]
            job = PurchaseJob("LON1", fl, "img-1", None, ["74.220.2"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, ssh_key=PUB, bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        b = st["created"][0]
        assert (b["network_id"], b["template_id"], b["initial_user"], b["ssh_key_id"]) == ("net-def", "img-1", "root", "key-9")
        assert BOT in b["script"] and PUB not in b["script"], "one key by id, the bot key by script"
        assert job.kept[0].ipv4 == ["74.220.2.2"] and set(st["regions_seen"]) == {"LON1"}
    finally:
        await srv.close()


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    await db.init_db()
    for m in (hetzner, vultr, linode, civo):
        m.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    _CACHE.clear()
    assert list(PROVIDERS) == ["hostvds", "digitalocean", "hetzner", "vultr", "linode", "civo", "gcp", "ovh"]
    assert {"selectel", "timeweb", "aeza", "serverspace"} <= set(PROVIDER_NAMES), "hidden, still usable for deletes"
    assert type(make_client("selectel", "", "t", "")).__name__ == "SelectelVDSClient"
    try:
        await hetzner_scenario()
        await vultr_scenario()
        await linode_scenario()
        await civo_scenario()
    finally:
        for m in (hetzner, vultr, linode, civo):
            m.asyncio.sleep = ORIG_SLEEP


def test_new_providers():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_new_providers()
    print("OK")
