"""IP-first purchases with reserved IPs (Vultr / Hetzner / Civo): servers are bought only for IPs from the
chosen subnets; reserved IP quota or ranges that never hit → the purchase goes on with servers; an account
instance limit makes the bot wait instead of stopping (or losing a found IP)."""
import asyncio
import itertools

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import buyer
from bot.buyer import PurchaseJob, run_purchase
from bot.providers import RESERVE_PROVIDERS, SCOUT_PROVIDERS, Flavor, ProviderError, Server, civo, vultr

ORIG_SLEEP = asyncio.sleep
FL = Flavor("small", "small", 1, 1024, 25)


async def nothing(job):
    await ORIG_SLEEP(0)


class FakeReserve:
    """A provider with reserved IPs (the Vultr / Civo shape) and its own server IP pool."""
    supports_ports = True
    reserves_ips = True
    max_held_ports = 0
    reserve_parallel = 3

    def __init__(self, reserved_ips, server_ips, quota=None, busy_creates=0):
        self.reserved_ips = itertools.cycle(reserved_ips)
        self.server_ips = itertools.cycle(server_ips)
        self.quota = quota            # reserved IPs allowed in total (None = no quota)
        self.busy_creates = busy_creates  # first N server creations hit the account limit
        self.ports, self.released, self.servers, self.deleted, self.created = {}, [], {}, [], []

    async def create_port(self, region, network_id, subnet_id):
        if self.quota is not None and len(self.ports) >= self.quota:
            raise ProviderError("reserved IP limit reached", 400)
        pid = f"p{len(self.ports)}"
        self.ports[pid] = next(self.reserved_ips)
        return pid, self.ports[pid]

    async def delete_port(self, region, port_id):
        self.released.append(port_id)

    async def create_server(self, region, name, flavor, image_id, network_id, boot, port_id=None, metadata=None,
                            ssh_key=None, extra_keys=None):
        if self.busy_creates:
            self.busy_creates -= 1
            raise ProviderError("You have reached the maximum number of active instances", 429)
        sid = f"s{len(self.servers)}"
        self.servers[sid] = self.ports[port_id] if port_id else next(self.server_ips)
        self.created.append((sid, port_id))
        return Server(sid, name, "BUILD", [], "pw")

    async def _get(self, sid):
        return Server(sid, "x", "ACTIVE", [self.servers[sid]], "pw")

    async def wait_ip(self, region, sid, timeout):
        return await self._get(sid)

    async def wait_active(self, region, sid, timeout):
        return await self._get(sid)

    async def delete_server(self, region, sid):
        self.deleted.append(sid)


def job(prefixes, count=1):
    return PurchaseJob("ewr", FL, "img", None, prefixes, count, max_attempts=0, parallel=2, active_timeout=5,
                       boot_volume_gb=10, port_parallel=3)


async def ip_first():
    """Only IPs are bought until one is in the subnet; one server, on that IP. No hunt limit by default."""
    c = FakeReserve(["8.8.1.1"] * 30 + ["45.32.3.3"], ["1.1.1.1"])
    j = job(["45.32.0.0/21"])
    assert j.max_port_attempts == 0, "no limit on IP checks"
    await run_purchase(c, j, nothing)
    assert [k.ipv4[0] for k in j.kept] == ["45.32.3.3"] and j.mode == "ports"
    assert len(c.created) == 1 and c.created[0][1] is not None, "a single server, built on the reserved IP"
    assert j.ports_checked >= 31 and not j.attempts - 1


async def never_hits():
    """Reserved IPs come from other ranges: after RESERVE_NO_MATCH the bot goes on with servers."""
    c = FakeReserve(["8.8.1.1"], ["9.9.9.1", "45.32.3.3"])
    j = job(["45.32.0.0/21"])
    await run_purchase(c, j, nothing)
    assert j.mode == "servers" and [k.ipv4[0] for k in j.kept] == ["45.32.3.3"]
    assert buyer.RESERVE_NO_MATCH <= j.ports_checked < buyer.RESERVE_NO_MATCH + 3
    assert any("ни одного из выбранных подсетей" in line for line in j.log)
    assert set(c.released) == set(c.ports), "every reserved IP released"


async def quota():
    """Reserved IP quota runs out mid-hunt (Vultr counts deleted ones for 24 h) → servers, not a stop."""
    c = FakeReserve(["8.8.1.1"], ["45.32.3.3"], quota=7)
    j = job(["45.32.0.0/21"])
    await run_purchase(c, j, nothing)
    assert not j.error and j.mode == "servers" and [k.ipv4[0] for k in j.kept] == ["45.32.3.3"]
    assert any("Лимит резервных IP" in line for line in j.log)


async def first_round_refused():
    c = FakeReserve(["8.8.1.1"], ["45.32.3.3"], quota=0)
    j = job(["45.32.0.0/21"])
    await run_purchase(c, j, nothing)
    assert not j.error and [k.ipv4[0] for k in j.kept] == ["45.32.3.3"]
    assert any("Резервные IP недоступны" in line for line in j.log)


async def instance_limit():
    """At the account's instance limit the found IP is kept and the server is built once there is room."""
    c = FakeReserve(["45.32.3.3"], ["1.1.1.1"], busy_creates=3)
    j = job(["45.32.0.0/21"])
    await run_purchase(c, j, nothing)
    assert [k.ipv4[0] for k in j.kept] == ["45.32.3.3"] and c.created[0][1] == "p0", "the same reserved IP"
    assert "p0" not in c.released and any("лимит аккаунта" in line for line in j.log)


async def server_mode_limit():
    """Server mode: the account stays at its limit → wait a while, then stop with a clear message."""
    c = FakeReserve([], ["1.1.1.1"], busy_creates=10 ** 6)
    c.supports_ports = False
    j = job([])
    await run_purchase(c, j, nothing)
    assert j.error and "не даёт создать сервер" in j.error and not j.kept


# ---------------- real clients against fake APIs ----------------
def vultr_limit_app():
    a = web.Application()
    a.router.add_post("/v2/instances", lambda r: web.json_response(
        {"error": "You have reached the maximum number of instances for this account", "status": 400}, status=400))
    a.router.add_post("/v2/reserved-ips", lambda r: web.json_response(
        {"error": "Reserved IP limit reached", "status": 400}, status=400))
    return a


async def vultr_limits():
    srv = TestServer(vultr_limit_app())
    await srv.start_server()
    try:
        async with vultr.VultrClient("t", str(srv.make_url("/v2"))) as c:
            try:
                await c.create_server("ewr", "x", FL, "2284", None, 10)
                raise AssertionError("expected an error")
            except ProviderError as e:
                assert e.status == 429, "instance limit = wait, not a fatal error"
            try:
                await c.create_port("ewr", None, None)
                raise AssertionError("expected an error")
            except ProviderError as e:
                assert e.status == 400, "reserved IP quota = switch to servers"
    finally:
        await srv.close()


def civo_app(addresses):
    addresses = itertools.cycle(addresses)
    st = {"ips": {}, "deleted_ips": [], "created": [], "deleted": [], "inst": {}}
    a = web.Application()
    a.router.add_get("/v2/networks", lambda r: web.json_response([{"id": "net-def", "default": True}]))
    a.router.add_get("/v2/quota", lambda r: web.json_response({"instance_count_usage": 3, "instance_count_limit": 5}))

    async def ips(req):
        if req.method == "POST":
            b = await req.json()
            assert b["region"] == "LON1"
            iid = f"ip-{len(st['ips'])}"
            st["ips"][iid] = {"id": iid, "name": b["name"], "ip": next(addresses), "assigned_to": {}}
            return web.json_response(st["ips"][iid])
        return web.json_response({"items": [v for k, v in st["ips"].items() if k not in st["deleted_ips"]]})
    a.router.add_route("*", "/v2/ips", ips)

    async def del_ip(req):
        st["deleted_ips"].append(req.match_info["id"])
        return web.json_response({"result": "success"})
    a.router.add_delete("/v2/ips/{id}", del_ip)

    async def instances(req):
        b = await req.json()
        st["created"].append(b)
        rip = next(v for v in st["ips"].values() if v["ip"] == b["reserved_ipv4"])
        rip["assigned_to"] = {"id": "inst-1", "type": "instance"}
        st["inst"]["inst-1"] = b["reserved_ipv4"]
        return web.json_response({"id": "inst-1", "hostname": b["hostname"], "status": "BUILDING"})
    a.router.add_post("/v2/instances", instances)

    async def one(req):
        if req.method == "DELETE":
            st["deleted"].append(req.match_info["id"])
            return web.json_response({"result": "success"})
        return web.json_response({"id": "inst-1", "status": "ACTIVE", "public_ip": st["inst"]["inst-1"],
                                  "initial_password": "civo-pass"})
    a.router.add_route("*", "/v2/instances/{id}", one)
    return a, st


async def civo_reserved():
    app, st = civo_app(["212.2.1.1", "212.2.9.9", "74.220.2.2"])
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with civo.CivoClient("t", str(srv.make_url("/v2"))) as c:
            assert await c.free_slots() == (2, 5)
            j = PurchaseJob("LON1", FL, "img-1", None, ["74.220.2"], 1, max_attempts=0, parallel=1,
                            active_timeout=10, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, j, nothing)
        assert j.mode == "ports" and [k.ipv4[0] for k in j.kept] == ["74.220.2.2"]
        assert len(st["created"]) == 1 and st["created"][0]["reserved_ipv4"] == "74.220.2.2"
        good = next(k for k, v in st["ips"].items() if v["ip"] == "74.220.2.2")
        assert set(st["deleted_ips"]) == set(st["ips"]) - {good}, "wrong reserved IPs released"
        # a later delete (new client, e.g. from «Мои серверы») also frees the reserved IP
        async with civo.CivoClient("t", str(srv.make_url("/v2"))) as c:
            await c.delete_server("LON1", "inst-1")
        assert st["deleted"] == ["inst-1"] and st["deleted_ips"][-1] == good
    finally:
        await srv.close()


def vultr_locked_app():
    st = {"tries": 0, "deleted": []}
    a = web.Application()
    a.router.add_get("/v2/reserved-ips", lambda r: web.json_response({"reserved_ips": [], "meta": {"links": {"next": ""}}}))

    async def delete(req):
        st["tries"] += 1
        if st["tries"] < 3:  # a fresh instance is locked while it installs
            return web.json_response({"error": "Server is currently locked", "status": 409}, status=409)
        st["deleted"].append(req.match_info["id"])
        return web.Response(status=204)
    a.router.add_delete("/v2/instances/{id}", delete)
    return a, st


async def vultr_locked():
    """'Server is currently locked' is not an error: the server is deleted in the background once unlocked."""
    app, st = vultr_locked_app()
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with vultr.VultrClient("t", str(srv.make_url("/v2"))) as c:
            await c.delete_server("ewr", "inst-9")  # returns at once, no exception
        await vultr.wait_pending(5)
        assert st["deleted"] == ["inst-9"] and st["tries"] == 3
    finally:
        await srv.close()


async def scenario():
    assert {"vultr", "hetzner", "civo"} <= RESERVE_PROVIDERS and "civo" in SCOUT_PROVIDERS
    orig_wait = buyer.BUSY_WAIT
    buyer.BUSY_WAIT = 0
    buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    civo.asyncio.sleep = vultr.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        await ip_first()
        await never_hits()
        await quota()
        await first_round_refused()
        await instance_limit()
        await server_mode_limit()
        await vultr_limits()
        await civo_reserved()
        await vultr_locked()
    finally:
        buyer.BUSY_WAIT = orig_wait
        buyer.asyncio.sleep = civo.asyncio.sleep = vultr.asyncio.sleep = ORIG_SLEEP


def test_reserve():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_reserve()
    print("OK")
