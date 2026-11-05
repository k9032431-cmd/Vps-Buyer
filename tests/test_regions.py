"""Region availability + flag detection against a fake OpenStack API."""
import asyncio
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import db, geo
from bot.config import config
from bot.hostvds import HostVDSClient

FLAVORS = {"eu-west1": [{"id": "1", "name": "s", "vcpus": 1, "ram": 1024, "disk": 10}], "eu-north1": [],
           "us-west": [{"id": "1", "name": "s", "vcpus": 1, "ram": 1024, "disk": 10}]}
LIMITS = {"eu-west1": (10, 0), "eu-north1": (10, 0), "us-west": (2, 2)}
SUBNETS = {"eu-west1": "94.103.80.0/24", "eu-north1": "123.45.0.0/24", "us-west": "207.1.2.0/24"}


def make_app():
    app = web.Application()

    async def tokens(req):
        base = str(req.url.origin())
        cat = [{"type": t, "endpoints": [{"interface": "public", "region_id": r, "url": f"{base}/{r}/{t}"}
                                         for r in FLAVORS]} for t in ("compute", "network")]
        return web.json_response({"token": {"expires_at": "2099-01-01T00:00:00Z", "catalog": cat}},
                                 status=201, headers={"X-Subject-Token": "t"})

    def r(req):
        return req.match_info["r"]

    app.router.add_post("/identity/v3/auth/tokens", tokens)
    app.router.add_get("/{r}/compute/flavors/detail", lambda q: web.json_response({"flavors": FLAVORS[r(q)]}))
    app.router.add_get("/{r}/compute/os-availability-zone", lambda q: web.json_response(
        {"availabilityZoneInfo": [{"zoneName": "nova", "zoneState": {"available": True}}]}))
    app.router.add_get("/{r}/compute/limits", lambda q: web.json_response({"limits": {"absolute": {
        "maxTotalInstances": LIMITS[r(q)][0], "totalInstancesUsed": LIMITS[r(q)][1],
        "maxTotalCores": 20, "totalCoresUsed": 0}}}))
    app.router.add_get("/{r}/network/v2.0/networks", lambda q: web.json_response(
        {"networks": [{"id": "pub", "router:external": True}]}))
    app.router.add_get("/{r}/network/v2.0/subnets", lambda q: web.json_response(
        {"subnets": [{"network_id": "pub", "ip_version": 4, "cidr": SUBNETS[r(q)]}]}))
    return app


async def scenario():
    tmp = tempfile.mkdtemp()
    object.__setattr__(config, "db_path", f"{tmp}/t.db")
    await db.init_db()
    looked_up = []

    async def fake_lookup(ip):
        looked_up.append(ip)
        return {"94.103.80.1": geo.Location("NL", "Amsterdam"), "207.1.2.1": geo.Location("US", "")}.get(ip)

    geo._lookup = fake_lookup
    server = TestServer(make_app())
    await server.start_server()
    try:
        async with HostVDSClient(str(server.make_url("/identity")), "u", "p", "u") as c:
            await c.authenticate()
            st = {s.region: s for s in await asyncio.gather(*(c.region_status(x) for x in c.regions()))}
        assert st["eu-west1"].available and st["eu-west1"].sample_ip == "94.103.80.1"
        assert not st["eu-north1"].available and st["eu-north1"].reason == "нет тарифов"
        assert not st["us-west"].available and st["us-west"].reason == "исчерпан лимит"

        loc = await geo.locate("hostvds", "eu-west1", "94.103.80.1")
        assert (loc.flag, loc.title) == ("🇳🇱", "Amsterdam"), loc
        loc = await geo.locate("hostvds", "us-west", "207.1.2.1")
        assert (loc.flag, loc.title) == ("🇺🇸", "США")
        # cached in DB: no second lookup
        await geo.locate("hostvds", "eu-west1", "94.103.80.1")
        assert looked_up == ["94.103.80.1", "207.1.2.1"]
        # unknown -> globe
        assert (await geo.locate("hostvds", "eu-north1", "123.45.0.1")).flag == "🌐"
        # manual override
        object.__setattr__(config, "region_names", "eu-north1=FI:Helsinki")
        loc = await geo.locate("hostvds", "eu-north1", None)
        assert (loc.flag, loc.title) == ("🇫🇮", "Helsinki")
        print({k: (v.available, v.reason) for k, v in st.items()})
    finally:
        await server.close()


def test_regions():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_regions()
    print("OK")
