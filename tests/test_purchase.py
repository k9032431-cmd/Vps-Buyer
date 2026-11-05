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
            {"id": "sub-" + c, "network_id": "pub", "ip_version": 4, "cidr": c} for c in ("94.103.80.0/24", "123.45.0.0/24", "207.1.2.0/24")
        ] + [{"network_id": "pub", "ip_version": 6, "cidr": "2a00::/64"}]})

    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_post("/compute/servers", create)
    app.router.add_get("/compute/servers/{id}", get)
    app.router.add_delete("/compute/servers/{id}", delete)
    app.router.add_get("/net/v2.0/networks", networks)
    app.router.add_get("/net/v2.0/subnets", subnets)
    return app, deleted


def make_port_app(ip_sequence):
    """Fake OpenStack with Neutron ports: IPs come from ip_sequence, or from the requested subnet."""
    app, _ = make_app(["0.0.0.0"])
    ips = iter(ip_sequence)
    st = {"ports": {}, "deleted_ports": [], "servers": {}, "deleted": [], "port_posts": 0}

    async def create_port(req):
        body = (await req.json())["port"]
        st["port_posts"] += 1
        fixed = body.get("fixed_ips")
        ip = f"95.182.87.{st['port_posts']}" if fixed else next(ips)
        pid = f"p{st['port_posts']}"
        st["ports"][pid] = ip
        return web.json_response({"port": {"id": pid, "fixed_ips": [{"ip_address": ip}]}}, status=201)

    async def delete_port(req):
        st["deleted_ports"].append(req.match_info["id"])
        return web.Response(status=204)

    async def list_ports(req):
        sid = req.query.get("device_id")
        return web.json_response({"ports": [{"id": p} for p, s in st.get("bound", {}).items() if s == sid]})

    async def create(req):
        body = (await req.json())["server"]
        port = body["networks"][0]["port"]
        sid = f"s{len(st['servers']) + 1}"
        st["servers"][sid] = st["ports"][port]
        st.setdefault("bound", {})[port] = sid
        return web.json_response({"server": {"id": sid, "adminPass": "pw"}}, status=202)

    async def get(req):
        sid = req.match_info["id"]
        return web.json_response({"server": {"id": sid, "name": sid, "status": "ACTIVE",
                                             "addresses": {"pub": [{"addr": st["servers"][sid], "version": 4}]}}})

    new = web.Application()
    for r in app.router.routes():
        path = r.resource.canonical
        if path.startswith("/compute/servers") or path.startswith("/net/v2.0/ports"):
            continue
        new.router.add_route(r.method, path, r.handler)
    new.router.add_post("/net/v2.0/ports", create_port)
    new.router.add_get("/net/v2.0/ports", list_ports)
    new.router.add_delete("/net/v2.0/ports/{id}", delete_port)
    new.router.add_post("/compute/servers", create)
    new.router.add_get("/compute/servers/{id}", get)

    async def delete(req):
        st["deleted"].append(req.match_info["id"])
        return web.Response(status=204)
    new.router.add_delete("/compute/servers/{id}", delete)
    return new, st


async def port_scenario():
    from bot.providers.hostvds import Flavor
    # 1) random network allocation: hunt until 2 IPs from 94.103.80.# show up
    app, st = make_port_app(["123.45.0.5", "207.1.2.9", "94.103.80.11", "123.45.0.6", "94.103.80.12"] +
                            [f"123.45.0.{i}" for i in range(10, 60)])
    server = TestServer(app)
    await server.start_server()
    try:
        async with HostVDSClient(str(server.make_url("/identity")), "u", "p", "u") as c:
            job = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["94.103.80"], 2,
                              max_attempts=20, parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=5)
            await run_purchase(c, job, lambda j: asyncio.sleep(0))
            print("\n".join(job.log))
            assert job.mode == "ports"
            assert sorted(s.ipv4[0] for s in job.kept) == ["94.103.80.11", "94.103.80.12"]
            assert job.attempts == 2 and job.deleted == 0, "servers bought only on matching IPs"
            assert job.ports_checked == 5
            assert sorted(st["deleted_ports"]) == ["p1", "p2", "p4"], st["deleted_ports"]  # wrong ones released
            assert "<code>94.103.80.11</code>" in "\n".join(job.log)
            # 2) exact /24 subnet → first try
            job2 = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["95.182.87"], 1,
                               max_attempts=20, parallel=1, active_timeout=30, boot_volume_gb=10,
                               targets=[("pub", "sub-95")], port_parallel=1)
            await run_purchase(c, job2, lambda j: asyncio.sleep(0))
            assert job2.ports_checked == 1 and job2.kept[0].ipv4[0].startswith("95.182.87.")
            # 3) several wanted blocks: any of them counts, each server is reported right away
            found = []
            async def on_found(srv):
                found.append(srv.ipv4[0])
            job3 = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["207.1.2", "123.45.0"], 3,
                               max_attempts=20, parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=2,
                               targets=[("pub", None), ("pub", None)], on_found=on_found)
            await run_purchase(c, job3, lambda j: asyncio.sleep(0))
            assert len(job3.kept) == 3 and len(found) == 3, job3.log
            assert all(ip.startswith(("207.1.2.", "123.45.0.")) for ip in found), found
            # 4) deleting a server also deletes its port
            await c.delete_server("eu-west2", job2.kept[0].id)
            assert st["deleted_ports"][-1] == "p6", st["deleted_ports"]  # port of job2 server
    finally:
        await server.close()


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
            assert [p.prefix for p in pools] == ["94.103.80", "123.45.0", "207.1.2"], pools
            assert pools[0].subnet_id == "sub-94.103.80.0/24"
            from bot.providers.hostvds import Flavor
            job = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["94"], 2,
                              max_attempts=20, parallel=1, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job, lambda j: orig_sleep(0))
            print("\n".join(job.log))
            assert [s.ipv4[0] for s in job.kept] == ["94.103.80.11", "94.103.80.12"]
            assert sorted(deleted) == ["s1", "s2", "s4"], deleted
            assert job.kept[0].admin_pass == "pws3"

            # attempt limit is respected
            job2 = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["8"], 1,
                               max_attempts=4, parallel=3, active_timeout=30, boot_volume_gb=10)
            await run_purchase(c, job2, lambda j: orig_sleep(0))
            assert job2.attempts == 4 and not job2.kept and job2.deleted == 4
    finally:
        await server.close()
        hv.asyncio.sleep = orig_sleep


def test_purchase_loop():
    asyncio.run(scenario())


def test_port_mode():
    asyncio.run(port_scenario())


if __name__ == "__main__":
    test_purchase_loop()
    test_port_mode()
    print("OK")
