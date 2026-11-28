"""UpCloud against a fake API 1.3 (shapes from upcloud-go-api): token or API user, zones, plans with
prices, cloud-init template rules (an SSH key always), server details for IPs, stop → delete."""
import asyncio
import base64

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import buyer
from bot.buyer import PurchaseJob, run_purchase
from bot.providers import PROVIDERS, make_client, upcloud

ORIG_SLEEP = asyncio.sleep
BOT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBotBotBotBotBotBotBotBotBotBotBotBotBot vps-buyer-bot"


def fake_upcloud(ips):
    ips = iter(ips)
    st = {"servers": {}, "created": [], "stopped": [], "deleted": [], "auth": [], "polls": {}}
    a = web.Application()

    @web.middleware
    async def auth(req, handler):
        st["auth"].append(req.headers.get("Authorization"))
        ok = req.headers.get("Authorization") in ("Bearer ucat_TEST", "Basic " + base64.b64encode(b"api-user:pw").decode())
        if not ok:
            return web.json_response({"error": {"error_code": "AUTHENTICATION_FAILED", "error_message": "bad"}}, status=401)
        return await handler(req)
    a.middlewares.append(auth)
    a.router.add_get("/1.3/account", lambda r: web.json_response({"account": {"credits": 9972.2324, "username": "me"}}))
    a.router.add_get("/1.3/zone", lambda r: web.json_response({"zones": {"zone": [
        {"id": "fi-hel1", "description": "Helsinki #1", "public": "yes", "trial_allowed": "no"},
        {"id": "se-sto1", "description": "Stockholm #1", "public": "yes"},
        {"id": "fi-hel1-private", "description": "private", "public": False}]}}))
    a.router.add_get("/1.3/plan", lambda r: web.json_response({"plans": {"plan": [
        {"name": "1xCPU-2GB", "core_number": 1, "memory_amount": 2048, "storage_size": 50, "storage_tier": "maxiops"},
        {"name": "DEV-1xCPU-1GB", "core_number": 1, "memory_amount": 1024, "storage_size": 20, "storage_tier": "standard"},
        {"name": "GPU-8xCPU-64GB-1xL40S", "core_number": 8, "memory_amount": 65536, "storage_size": 100}]}}))
    a.router.add_get("/1.3/price", lambda r: web.json_response({"prices": {"zone": [
        {"name": "fi-hel1", "server_plan_1xCPU-2GB": {"amount": 1, "price": 2.2321},
         "server_plan_DEV-1xCPU-1GB": {"amount": 1, "price": 0.744}, "ipv4_address": {"amount": 1, "price": 0.3}}]}}))
    a.router.add_get("/1.3/storage/template", lambda r: web.json_response({"storages": {"storage": [
        {"uuid": "t-win", "title": "Windows Server 2022 Standard", "type": "template"},
        {"uuid": "t-u24", "title": "Ubuntu Server 24.04 LTS (Noble Numbat)", "type": "template", "template_type": "cloud-init"},
        {"uuid": "t-arch", "title": "Arch Linux", "type": "template"}]}}))

    def detail(uuid):
        s = st["servers"][uuid]
        st["polls"][uuid] = st["polls"].get(uuid, 0) + 1
        if s["state"] == "maintenance" and st["polls"][uuid] > 1:
            s["state"] = "started"
        return {"uuid": uuid, "title": s["title"], "zone": s["zone"], "state": s["state"],
                "ip_addresses": {"ip_address": [{"access": "utility", "address": "10.1.0.5", "family": "IPv4"},
                                                {"access": "public", "address": s["ip"], "family": "IPv4"},
                                                {"access": "public", "address": "2a04::1", "family": "IPv6"}]}}

    async def servers(req):
        if req.method == "GET":
            return web.json_response({"servers": {"server": [
                {"uuid": u, "title": s["title"], "zone": s["zone"], "state": s["state"]}
                for u, s in st["servers"].items() if u not in st["deleted"]]}})
        b = (await req.json())["server"]
        st["created"].append(b)
        uuid = f"00{len(st['servers'])}-uuid"
        st["servers"][uuid] = {"title": b["title"], "zone": b["zone"], "state": "maintenance", "ip": next(ips)}
        return web.json_response({"server": {**detail(uuid), "password": "should-not-be-used"}}, status=202)
    a.router.add_route("*", "/1.3/server", servers)

    async def server(req):
        uuid = req.match_info["uuid"]
        if uuid not in st["servers"] or uuid in st["deleted"]:
            return web.json_response({"error": {"error_code": "SERVER_NOT_FOUND", "error_message": "no"}}, status=404)
        if req.method == "DELETE":
            if st["servers"][uuid]["state"] != "stopped":
                return web.json_response({"error": {"error_code": "SERVER_STATE_ILLEGAL", "error_message": "stop it"}},
                                         status=409)
            assert req.query.get("storages") == "1"
            st["deleted"].append(uuid)
            return web.Response(status=204)
        return web.json_response({"server": detail(uuid)})
    a.router.add_route("*", "/1.3/server/{uuid}", server)

    async def stop(req):
        uuid = req.match_info["uuid"]
        assert (await req.json())["stop_server"]["stop_type"] == "hard"
        st["stopped"].append(uuid)
        st["servers"][uuid]["state"] = "stopped"
        return web.json_response({"server": detail(uuid)})
    a.router.add_post("/1.3/server/{uuid}/stop", stop)
    return a, st


async def scenario():
    assert "upcloud" in PROVIDERS
    assert upcloud._auth("api-user:pw").startswith("Basic ") and upcloud._auth("ucat_ABC") == "Bearer ucat_ABC"
    app, st = fake_upcloud(["94.237.1.1", "94.237.2.2", "5.22.9.9"])
    srv = TestServer(app)
    await srv.start_server()
    url = str(srv.make_url("/1.3"))
    try:
        assert type(make_client("upcloud", "", "ucat_TEST", "")).__name__ == "UpCloudClient"
        async with upcloud.UpCloudClient("api-user:pw", url) as c:
            assert await c.check() == "me, баланс 99.72"
        async with upcloud.UpCloudClient("ucat_TEST", url) as c:
            regs = {r.region: r for r in await c.region_statuses()}
            assert set(regs) == {"fi-hel1", "se-sto1"} and regs["fi-hel1"].country == "FI"
            assert regs["fi-hel1"].city.endswith("🧪 не для пробного") and "🧪" not in regs["se-sto1"].city
            flavors = await c.list_flavors("fi-hel1")
            assert [f.id for f in flavors] == ["DEV-1xCPU-1GB", "1xCPU-2GB"], "GPU hidden, cheapest first"
            assert flavors[0].kind == "standard" and "0.0074/час" in flavors[0].price
            images = await c.list_images("fi-hel1")
            assert images[0].id == "t-u24" and all("Windows" not in i.name for i in images)

            job = PurchaseJob("fi-hel1", flavors[0], "t-u24", None, ["5.22.9"], 1, max_attempts=0, parallel=1,
                              active_timeout=10, boot_volume_gb=10, bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert [k.ipv4 for k in job.kept] == [["5.22.9.9"]], job.log
            kept = job.kept[0]
            assert kept.admin_pass and kept.admin_pass != "should-not-be-used", "our password, set by the script"
            b = st["created"][0]
            assert b["login_user"]["ssh_keys"]["ssh_key"] == [BOT] and b["login_user"]["create_password"] == "no"
            assert b["storage_devices"]["storage_device"][0]["tier"] == "standard"
            assert b["networking"]["interfaces"]["interface"][0]["type"] == "public"
            assert "chpasswd" in b["user_data"] and "PasswordAuthentication yes" in b["user_data"]
            assert [s.ipv4 for s in await c.list_servers() if s.id == kept.id] == [["5.22.9.9"]], "IPs from details"
        await upcloud.wait_pending(5)
        wrong = [u for u in st["servers"] if u != kept.id]
        assert set(st["stopped"]) == set(wrong) and set(st["deleted"]) == set(wrong), "stopped, then deleted"
    finally:
        await srv.close()


def test_upcloud():
    orig = buyer.asyncio.sleep
    upcloud.asyncio.sleep = buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        asyncio.run(scenario())
    finally:
        upcloud.asyncio.sleep = buyer.asyncio.sleep = orig


if __name__ == "__main__":
    test_upcloud()
    print("OK")
