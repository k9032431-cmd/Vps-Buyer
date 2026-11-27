"""OVHcloud Public Cloud against a fake OpenStack: Keystone scoped by project id, Ext-Net ports first
(even when OVH hides its subnets), servers built on the right port, regions with flags, flavors."""
import asyncio
import itertools

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import buyer
from bot.buyer import PurchaseJob, run_purchase
from bot.providers import PROVIDERS, make_client, ovh

ORIG_SLEEP = asyncio.sleep
PROJECT = "0123456789abcdef0123456789abcdef"


def fake_ovh(port_ips, allow_ports=True):
    port_ips = itertools.cycle(port_ips)
    st = {"ports": {}, "deleted_ports": [], "servers": {}, "deleted": [], "scope": None, "sg": []}
    a = web.Application()

    async def tokens(req):
        b = await req.json()
        st["scope"] = b["auth"]["scope"]
        base = str(req.url.origin())
        cat = [{"type": t, "endpoints": [{"interface": "public", "region_id": r, "url": f"{base}/{r}/{t}"}
                                         for r in ("GRA11", "WAW1")]} for t in ("compute", "network", "image")]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}}, status=201,
                                 headers={"X-Subject-Token": "t"})
    a.router.add_post("/v3/auth/tokens", tokens)

    async def networks(req):
        nets = [{"id": "ext", "name": "Ext-Net", "router:external": True}]
        if req.query.get("name") and req.query["name"] != "Ext-Net":
            nets = []
        return web.json_response({"networks": nets})
    a.router.add_get("/{r}/network/v2.0/networks", networks)
    # OVH shows a placeholder instead of its real ranges
    a.router.add_get("/{r}/network/v2.0/subnets", lambda r: web.json_response({"subnets": [
        {"id": "sub-x", "network_id": "ext", "ip_version": 4, "cidr": "0.0.0.0/0",
         "allocation_pools": [{"start": "0.0.0.2", "end": "0.0.255.254"}]}]}))

    async def sgs(req):
        if req.method == "POST":
            st["sg"].append("sg1")
            return web.json_response({"security_group": {"id": "sg1"}}, status=201)
        return web.json_response({"security_groups": [{"id": s} for s in st["sg"]]})
    a.router.add_route("*", "/{r}/network/v2.0/security-groups", sgs)
    a.router.add_post("/{r}/network/v2.0/security-group-rules", lambda r: web.json_response({}, status=201))

    async def ports(req):
        if req.method == "POST":
            if not allow_ports:
                return web.json_response({"NeutronError": {"message": "not allowed on Ext-Net"}}, status=403)
            b = (await req.json())["port"]
            assert b["network_id"] == "ext"
            pid = f"p{len(st['ports'])}"
            st["ports"][pid] = next(port_ips)
            return web.json_response({"port": {"id": pid, "fixed_ips": [{"ip_address": st["ports"][pid]},
                                                                        {"ip_address": "2001:41d0::1"}]}}, status=201)
        dev = req.query.get("device_id")
        return web.json_response({"ports": [{"id": p} for p, s in st["servers"].items() if s.get("port") == p and dev]})
    a.router.add_route("*", "/{r}/network/v2.0/ports", ports)

    async def del_port(req):
        st["deleted_ports"].append(req.match_info["id"])
        return web.Response(status=204)
    a.router.add_delete("/{r}/network/v2.0/ports/{id}", del_port)
    a.router.add_get("/{r}/compute/flavors/detail", lambda r: web.json_response({"flavors": [
        {"id": "f-b27", "name": "b2-7", "vcpus": 2, "ram": 7000, "disk": 50},
        {"id": "f-win", "name": "win-b2-7", "vcpus": 2, "ram": 7000, "disk": 50},
        {"id": "f-d22", "name": "d2-2", "vcpus": 1, "ram": 2000, "disk": 25},
        {"id": "f-gpu", "name": "t1-45", "vcpus": 8, "ram": 45000, "disk": 400}]}))
    a.router.add_get("/{r}/compute/os-availability-zone", lambda r: web.json_response(
        {"availabilityZoneInfo": [{"zoneState": {"available": True}}]}))
    a.router.add_get("/{r}/compute/limits", lambda r: web.json_response(
        {"limits": {"absolute": {"maxTotalInstances": 20, "totalInstancesUsed": 0}}}))

    async def servers(req):
        b = (await req.json())["server"]
        sid = f"s{len(st['servers'])}"
        net = b["networks"][0]
        ip = st["ports"][net["port"]] if "port" in net else "57.128.0.9"
        st["servers"][sid] = {"body": b, "ip": ip, "port": net.get("port")}
        return web.json_response({"server": {"id": sid}}, status=202)
    a.router.add_post("/{r}/compute/servers", servers)

    async def server(req):
        sid = req.match_info["id"]
        if req.method == "DELETE":
            st["deleted"].append(sid)
            return web.Response(status=204)
        ip = st["servers"][sid]["ip"]
        return web.json_response({"server": {"id": sid, "name": "x", "status": "ACTIVE", "addresses": {
            "Ext-Net": [{"addr": ip, "version": 4}, {"addr": "2001:41d0::1", "version": 6}]}}})
    a.router.add_route("*", "/{r}/compute/servers/{id}", server)
    return a, st


async def scenario():
    assert "ovh" in PROVIDERS and ovh.city("GRA11") == ("FR", "Гравлин") and ovh.city("WAW1") == ("PL", "Варшава")
    assert ovh.city("EU-SOUTH-MIL") == ("IT", "Милан")
    app, st = fake_ovh(["51.75.1.1", "51.75.2.2", "57.129.7.7"])
    srv = TestServer(app)
    await srv.start_server()
    url = str(srv.make_url("")).rstrip("/")
    try:
        assert type(make_client("ovh", "user-x", "pw", PROJECT)).__name__ == "OVHClient"
        async with ovh.OVHClient(url, "user-x", "pw", PROJECT) as c:
            assert (await c.check()).startswith("OVH, регионы: GRA11, WAW1")
            assert st["scope"] == {"project": {"id": PROJECT}}, "scoped by project id"
            regs = {r.region: r for r in await c.region_statuses()}
            assert regs["GRA11"].available and regs["GRA11"].country == "FR" and regs["WAW1"].city == "Варшава"
            flavors = await c.list_flavors("GRA11")
            assert [f.name for f in flavors] == ["d2-2", "b2-7"], "windows and GPU flavors hidden"
            assert flavors[0].price == "≈€6/мес"
            assert await c.ip_pools("GRA11") == [], "no fake 0.0.x.# blocks"

            # subnets hidden: ports still go on Ext-Net → only the matching IP gets a server
            job = PurchaseJob("GRA11", flavors[0], "img", None, ["57.129.7"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert job.mode == "ports" and [k.ipv4[0] for k in job.kept] == ["57.129.7.7"], job.log
            assert len(st["servers"]) == 1 and next(iter(st["servers"].values()))["port"] is not None
            good = next(p for p, ip in st["ports"].items() if ip == "57.129.7.7")
            assert set(st["deleted_ports"]) == set(st["ports"]) - {good}
            # scouting: IPs only, no servers; the blocks then show up in the IP picker with their counts
            from bot.handlers.buy import _merge_seen
            from bot.scanner import ScanJob, run_scan
            before = len(st["servers"])
            scan = ScanJob("GRA11", flavors[0], "img", 4)
            await run_scan(c, scan, lambda j: ORIG_SLEEP(0))
            assert len(scan.ips) == 4 and len(st["servers"]) == before, "no server bought while scouting"
            pools = {p.prefix: p.hits for p in _merge_seen([], scan.ips)}
            assert pools["51.75.1"] >= 1 and set(pools) <= {"51.75.1", "51.75.2", "57.129.7"}
            assert (await c.cheapest_plan("GRA11"))[1] == ovh.PRICES["d2-2"], "per hour"
            assert "chpasswd" in __import__("base64").b64decode(
                next(iter(st["servers"].values()))["body"]["user_data"]).decode()
    finally:
        await srv.close()

    # OVH refuses ports on Ext-Net → servers on Ext-Net instead, not a stop
    app, st = fake_ovh(["51.75.1.1"], allow_ports=False)
    srv = TestServer(app)
    await srv.start_server()
    url = str(srv.make_url("")).rstrip("/")
    try:
        async with ovh.OVHClient(url, "user-x", "pw", PROJECT) as c:
            fl = (await c.list_flavors("GRA11"))[0]
            job = PurchaseJob("GRA11", fl, "img", None, ["57.128.0"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        assert not job.error and job.mode == "servers" and [k.ipv4[0] for k in job.kept] == ["57.128.0.9"], job.log
        body = next(iter(st["servers"].values()))["body"]
        assert body["networks"] == [{"uuid": "ext"}], "server mode puts the server on Ext-Net"
        # scouting where ports are refused: falls back to cheapest servers
        from bot.scanner import ScanJob, run_scan
        async with ovh.OVHClient(url, "user-x", "pw", PROJECT) as c:
            scan = ScanJob("GRA11", fl, "img", 2)
            await run_scan(c, scan, lambda j: ORIG_SLEEP(0))
        assert scan.no_ports and scan.ips == ["57.128.0.9", "57.128.0.9"] and scan.deleted == 2, scan.log
    finally:
        await srv.close()


def test_ovh():
    orig = buyer.asyncio.sleep
    buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        asyncio.run(scenario())
    finally:
        buyer.asyncio.sleep = orig


if __name__ == "__main__":
    test_ovh()
    print("OK")
