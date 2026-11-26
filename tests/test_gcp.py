"""Google Cloud against a fake Compute Engine API: service account JWT → token, static IP first, VM built
with it as its external IP, firewall, startup script (password, keys, ports), operations, cleanup."""
import asyncio
import base64
import itertools
import json
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Document, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bot import buyer, db
from bot.buyer import PurchaseJob, run_purchase
from bot.config import config
from bot.providers import PROVIDERS, RESERVE_PROVIDERS, gcp, make_client

ORIG_SLEEP = asyncio.sleep
BOT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBotBotBotBotBotBotBotBotBotBotBotBotBot vps-buyer-bot"
PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY = {"type": "service_account", "project_id": "my-proj", "private_key_id": "k1",
       "private_key": PRIV.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode(),
       "client_email": "bot@my-proj.iam.gserviceaccount.com"}


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def fake_gcp(static_ips):
    static_ips = itertools.cycle(static_ips)
    st = {"addresses": {}, "deleted_addr": [], "instances": {}, "deleted": [], "firewalls": {}, "polls": {},
          "tokens": 0, "quota": None}
    a = web.Application()

    async def token(req):
        form = await req.post()
        head, claims, sig = form["assertion"].split(".")
        PRIV.public_key().verify(b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        c = json.loads(b64d(claims))
        assert c["iss"] == KEY["client_email"] and c["scope"].endswith("/auth/compute")
        st["tokens"] += 1
        return web.json_response({"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"})
    a.router.add_post("/token", token)

    def op(kind, target, error=None):
        o = {"name": f"op-{kind}", "status": "DONE", "operationType": kind, "targetLink": target,
             "selfLink": f"{str(req_base[0])}/op"}
        if error:
            o["error"] = {"errors": [error]}
        return o
    req_base = [""]

    @web.middleware
    async def auth(req, handler):
        req_base[0] = str(req.url.origin()) + "/compute/v1/projects/my-proj"
        if req.path.startswith("/compute") and req.headers.get("Authorization") != "Bearer tok":
            return web.json_response({"error": {"code": 401, "message": "bad token"}}, status=401)
        return await handler(req)
    a.middlewares.append(auth)
    P = "/compute/v1/projects/my-proj"
    a.router.add_get(P, lambda r: web.json_response({"name": "my-proj"}))
    a.router.add_get(P + "/op", lambda r: web.json_response({"status": "DONE"}))
    a.router.add_get(P + "/regions", lambda r: web.json_response({"items": [
        {"name": "europe-north1", "status": "UP", "zones": [f"{P}/zones/europe-north1-a", f"{P}/zones/europe-north1-b"]},
        {"name": "europe-central2", "status": "DOWN", "zones": []}]}))

    async def mtypes(req):
        zone = req.match_info["zone"]
        items = [{"name": "e2-small", "guestCpus": 2, "memoryMb": 2048}, {"name": "e2-micro", "guestCpus": 2, "memoryMb": 1024},
                 {"name": "e2-custom-4-8192", "guestCpus": 4, "memoryMb": 8192}]
        return web.json_response({"items": items if zone.endswith("-a") else items[:1]})
    a.router.add_get(P + "/zones/{zone}/machineTypes", mtypes)

    async def addresses(req):
        region = req.match_info["region"]
        if req.method == "POST":
            b = await req.json()
            if st["quota"] is not None and len(st["addresses"]) >= st["quota"]:
                return web.json_response(op("insert", f"{P}/regions/{region}/addresses/{b['name']}",
                                            {"code": "QUOTA_EXCEEDED", "message": "Quota 'STATIC_ADDRESSES' exceeded"}))
            st["addresses"][b["name"]] = {"name": b["name"], "address": next(static_ips), "status": "RESERVED"}
            return web.json_response(op("insert", f"{P}/regions/{region}/addresses/{b['name']}"))
        return web.json_response({"items": [v for k, v in st["addresses"].items() if k not in st["deleted_addr"]]})
    a.router.add_route("*", P + "/regions/{region}/addresses", addresses)

    async def address(req):
        name = req.match_info["name"]
        if req.method == "DELETE":
            if any(i.get("natIP") == st["addresses"][name]["address"] and k not in st["deleted"]
                   for k, i in st["instances"].items()):
                return web.json_response({"error": {"code": 400, "message": "is being used by instance",
                                                    "errors": [{"reason": "resourceInUseByAnotherResource"}]}}, status=400)
            st["deleted_addr"].append(name)
            return web.json_response(op("delete", name))
        return web.json_response(st["addresses"][name])
    a.router.add_route("*", P + "/regions/{region}/addresses/{name}", address)

    async def firewall(req):
        name = req.match_info.get("name")
        if req.method == "POST":
            b = await req.json()
            st["firewalls"][b["name"]] = b
            return web.json_response(op("insert", b["name"]))
        if name not in st["firewalls"]:
            return web.json_response({"error": {"code": 404, "message": "not found"}}, status=404)
        return web.json_response(st["firewalls"][name])
    a.router.add_post(P + "/global/firewalls", firewall)
    a.router.add_get(P + "/global/firewalls/{name}", firewall)

    async def instances(req):
        zone = req.match_info["zone"]
        b = await req.json()
        nat = b["networkInterfaces"][0]["accessConfigs"][0].get("natIP") or "35.200.0.9"
        st["instances"][b["name"]] = {"body": b, "natIP": nat, "zone": zone}
        return web.json_response(op("insert", b["name"]))
    a.router.add_post(P + "/zones/{zone}/instances", instances)

    def inst_json(name):
        i = st["instances"][name]
        st["polls"][name] = st["polls"].get(name, 0) + 1
        return {"name": name, "zone": f"{P}/zones/{i['zone']}", "status": "RUNNING" if st["polls"][name] > 1 else "STAGING",
                "networkInterfaces": [{"accessConfigs": [{"natIP": i["natIP"]}]}]}

    async def instance(req):
        name = req.match_info["name"]
        if name not in st["instances"] or name in st["deleted"]:
            return web.json_response({"error": {"code": 404, "message": "not found"}}, status=404)
        if req.method == "DELETE":
            st["deleted"].append(name)
            return web.json_response(op("delete", name))
        return web.json_response(inst_json(name))
    a.router.add_route("*", P + "/zones/{zone}/instances/{name}", instance)
    a.router.add_get(P + "/aggregated/instances", lambda r: web.json_response({"items": {
        "zones/europe-north1-a": {"instances": [inst_json(n) for n in st["instances"] if n not in st["deleted"]]},
        "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}}}}))
    return a, st


async def scenario():
    assert "gcp" in PROVIDERS and "gcp" in RESERVE_PROVIDERS
    try:
        gcp.parse_key('{"type": "authorized_user"}')
        raise AssertionError("user credentials accepted")
    except gcp.ProviderError:
        pass
    app, st = fake_gcp(["34.88.1.1", "34.88.2.2", "35.228.7.7"])
    srv = TestServer(app)
    await srv.start_server()
    api, tok = str(srv.make_url("/compute/v1")), str(srv.make_url("/token"))
    secret = json.dumps(KEY)
    assert type(make_client("gcp", "", secret, "")).__name__ == "GCPClient"
    try:
        async with gcp.GCPClient(secret, api, tok) as c:
            assert (await c.check()).startswith("my-proj, ")
            regs = {r.region: r for r in await c.region_statuses()}
            assert regs["europe-north1"].available and regs["europe-north1"].country == "FI"
            assert not regs["europe-central2"].available
            flavors = await c.list_flavors("europe-north1")
            assert [f.id for f in flavors] == ["europe-north1-a/e2-micro", "europe-north1-a/e2-small"], flavors
            assert flavors[0].ram_mb == 1024 and "≈$7/мес" in flavors[0].price
            images = await c.list_images("europe-north1")
            assert images[0].id == "ubuntu-os-cloud/ubuntu-2204-lts"

            job = PurchaseJob("europe-north1", flavors[0], images[0].id, None, ["35.228.7"], 1, max_attempts=0,
                              parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=3, bot_key=("k", BOT))
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            assert [k.ipv4[0] for k in job.kept] == ["35.228.7.7"], job.log
            kept = job.kept[0]
            assert kept.admin_pass and kept.id.startswith("europe-north1-a/") and len(st["instances"]) == 1
            body = next(iter(st["instances"].values()))["body"]
            assert body["networkInterfaces"][0]["accessConfigs"][0]["natIP"] == "35.228.7.7"
            assert body["tags"]["items"] == ["vps-buyer"]
            assert body["disks"][0]["initializeParams"]["sourceImage"] == \
                "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
            script = body["metadata"]["items"][0]["value"]
            assert BOT in script and "chpasswd" in script and "PasswordAuthentication yes" in script
            fw = st["firewalls"]["vps-buyer-allow-all"]
            assert fw["allowed"] == [{"IPProtocol": "all"}] and fw["sourceRanges"] == ["0.0.0.0/0"]
            good = next(k for k, v in st["addresses"].items() if v["address"] == "35.228.7.7")
            assert set(st["deleted_addr"]) == set(st["addresses"]) - {good}, "wrong static IPs released"
            assert [s.id for s in await c.list_servers()] == [kept.id]
            await c.delete_server("", kept.id)
        await gcp.wait_pending(5)
        assert st["deleted"] and good in st["deleted_addr"], "VM and its static IP are gone"

        # static IP quota used up → servers with an ephemeral IP, not a stop
        st["quota"] = len(st["addresses"])
        async with gcp.GCPClient(secret, api, tok) as c:
            job = PurchaseJob("europe-north1", flavors[0], images[0].id, None, ["35.200.0"], 1, max_attempts=0,
                              parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=3)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        assert not job.error and job.mode == "servers" and [k.ipv4[0] for k in job.kept] == ["35.200.0.9"], job.log
    finally:
        await srv.close()

    # connecting the account: the key arrives as a .json document
    from bot.handlers import accounts
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    await db.init_db()
    saved = {}

    async def fake_save(message, state, username, secret_, project):
        saved["secret"] = secret_
    orig_save, accounts._save = accounts._save, fake_save
    try:
        class FakeBot:
            async def download(self, doc):
                import io
                return io.BytesIO(json.dumps(KEY, indent=2).encode())
        user = User(id=1, is_bot=False, first_name="A")
        msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"), from_user=user,
                      document=Document(file_id="f", file_unique_id="u", file_name="key.json", file_size=2400))

        class FakeState:
            async def get_data(self):
                return {"provider": "gcp"}
        await accounts.add_token(msg, FakeState(), FakeBot())
        assert json.loads(saved["secret"]) == KEY and "\n" not in saved["secret"]
    finally:
        accounts._save = orig_save


def test_gcp():
    orig = buyer.asyncio.sleep
    gcp.asyncio.sleep = buyer.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    try:
        asyncio.run(scenario())
    finally:
        gcp.asyncio.sleep = buyer.asyncio.sleep = orig


if __name__ == "__main__":
    test_gcp()
    print("OK")
