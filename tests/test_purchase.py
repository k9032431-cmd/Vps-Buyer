"""End-to-end test of the purchase loop against a fake OpenStack API."""
import asyncio
import itertools

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.buyer import PurchaseJob, run_purchase
from bot.providers.hostvds import HostVDSClient


def make_app(ip_sequence):
    ips = itertools.cycle(ip_sequence)
    servers, deleted = {}, []
    app = web.Application()

    async def tokens(req):
        base = str(req.url.origin())
        catalog = [
            {"type": "compute", "endpoints": [{"interface": "public", "region_id": "eu-west2", "url": base + "/compute"}]},
            {"type": "network", "endpoints": [{"interface": "public", "region_id": "eu-west2", "url": base + "/net"}]},
        ]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": catalog}},
                                 status=201, headers={"X-Subject-Token": "tok"})

    async def create(req):
        body = await req.json()
        sid = f"s{len(servers) + 1}"
        servers[sid] = {"id": sid, "name": body["server"]["name"], "polls": 0, "ip": next(ips)}
        return web.json_response({"server": {"id": sid, "adminPass": "pw" + sid}}, status=202)

    async def get(req):
        s = servers[req.match_info["id"]]
        s["polls"] += 1
        active = s["polls"] > 1
        addrs = {"public": [{"addr": s["ip"], "version": 4}]} if active else {}
        return web.json_response({"server": {"id": s["id"], "name": s["name"],
                                             "status": "ACTIVE" if active else "BUILD", "addresses": addrs}})

    async def delete(req):
        deleted.append(req.match_info["id"])
        return web.Response(status=204)

    async def networks(req):
        return web.json_response({"networks": [{"id": "pub", "router:external": True}]})

    async def subnets(req):
        return web.json_response({"subnets": [
            {"network_id": "pub", "ip_version": 4, "cidr": c} for c in ("94.103.80.0/24", "123.45.0.0/24", "207.1.2.0/24")
        ] + [{"network_id": "pub", "ip_version": 6, "cidr": "2a00::/64"}]})

    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_post("/compute/servers", create)
    app.router.add_get("/compute/servers/{id}", get)
    app.router.add_delete("/compute/servers/{id}", delete)
    app.router.add_get("/net/v2.0/networks", networks)
    app.router.add_get("/net/v2.0/subnets", subnets)
    return app, deleted


async def scenario():
    import bot.providers.hostvds as hv
    orig_sleep = hv.asyncio.sleep
    hv.asyncio.sleep = lambda s: orig_sleep(0)  # speed up polling
    app, deleted = make_app(["123.45.0.5", "207.1.2.9", "94.103.80.11", "123.45.0.6", "94.103.80.12"])
    server = TestServer(app)
    await server.start_server()
    try:
        async with HostVDSClient(str(server.make_url("/identity")), "u", "p", "u") as c:
            await c.authenticate()
            assert c.regions() == ["eu-west2"]
            pools = await c.ip_pools("eu-west2")
            assert [p.prefix for p in pools] == ["94", "123", "207"], pools
            from bot.providers.hostvds import Flavor
            job = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", "94", 2,
                              max_attempts=20, parallel=1, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: orig_sleep(0))
            print("\n".join(job.log))
            assert [s.ipv4[0] for s in job.kept] == ["94.103.80.11", "94.103.80.12"]
            assert sorted(deleted) == ["s1", "s2", "s4"], deleted
            assert job.kept[0].admin_pass == "pws3"

            # attempt limit is respected
            job2 = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", "8", 1,
                               max_attempts=4, parallel=3, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job2, lambda j: orig_sleep(0))
            assert job2.attempts == 4 and not job2.kept and job2.deleted == 4
    finally:
        await server.close()
        hv.asyncio.sleep = orig_sleep


def test_purchase_loop():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_purchase_loop()
    print("OK")
