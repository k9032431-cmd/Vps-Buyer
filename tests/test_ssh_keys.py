"""Access step: password or a saved SSH key; the key reaches the server at creation."""
import asyncio
import base64
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric import ed25519

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.buyer import PurchaseJob, run_purchase  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import Flavor, digitalocean, selectel  # noqa: E402
from bot.providers.digitalocean import DigitalOceanClient  # noqa: E402
from bot.providers.selectel import SelectelVDSClient  # noqa: E402
from tests import test_digitalocean as do_t, test_selectel as sel_t  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402

ORIG_SLEEP = asyncio.sleep
KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(S.Encoding.OpenSSH, S.PublicFormat.OpenSSH).decode() + " me@laptop"
PRIV = KEY.private_bytes(S.Encoding.PEM, S.PrivateFormat.OpenSSH, S.NoEncryption()).decode()


def text_update(n, text):
    m = Message(message_id=n, date=datetime.now(), chat=Chat(id=UID, type="private"),
                from_user=User(id=UID, is_bot=False, first_name="A"), text=text)
    return Update(update_id=n, message=m)


def markup(s):
    last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
    return [b.text for row in last.reply_markup.inline_keyboard for b in row]


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    s = FakeSession()
    bot = Bot("1:x", session=s)
    dp = Dispatcher()
    dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
    ctx = dp.fsm.get_context(bot, UID, UID)
    flavor = {"id": "s-1vcpu-1gb", "name": "s-1vcpu-1gb", "vcpus": 1, "ram_mb": 1024, "disk_gb": 25, "price": ""}
    base_state = {"provider": "digitalocean", "account_id": 1, "region": "fra1",
                  "region_label": "🇩🇪 Франкфурт · fra1", "prefixes": ["142.93.96.0/20"], "count": 1,
                  "flavor": flavor, "images": [{"id": "ubuntu-24-04-x64", "name": "🟠 Ubuntu 24.04"}]}

    # ---- the access step appears after the OS ----
    await ctx.set_data(base_state)
    await dp.feed_update(bot, cb_update(1, "buy:img:0"))
    assert "Как входить на сервер" in s.texts()[-1]
    assert markup(s)[:2] == ["🔑 Пароль root", "➕ Добавить SSH-ключ"]
    await dp.feed_update(bot, cb_update(2, "buy:auth:add"))
    assert "публичный" in s.texts()[-1]
    await dp.feed_update(bot, text_update(3, PRIV))                       # private key → refused + deleted
    assert "Это приватный ключ" in s.texts()[-1]
    assert any(type(c).__name__ == "DeleteMessage" for c in s.calls)
    await dp.feed_update(bot, text_update(4, "hello"))
    assert "не похоже на публичный SSH-ключ" in s.texts()[-1]
    await dp.feed_update(bot, text_update(5, PUB))
    assert "Ключ «me@laptop» сохранён" in s.texts()[-2]
    confirm = s.texts()[-1]
    assert "🔐 Доступ: <b>SSH-ключ «me@laptop»</b>" in confirm, confirm
    keys = await db.list_ssh_keys(UID)
    assert [(k[1], k[2]) for k in keys] == [("me@laptop", PUB)]

    # ---- next purchase: the saved key is a button; password is still possible ----
    await ctx.set_data(base_state)
    await dp.feed_update(bot, cb_update(6, "buy:img:0"))
    assert "🗝 SSH-ключ «me@laptop»" in markup(s)
    await dp.feed_update(bot, cb_update(7, "buy:auth:pw"))
    assert "🔐 Доступ: <b>пароль root</b>" in s.texts()[-1]

    # ---- buy:go hands the key to the purchase ----
    await ctx.set_data({**base_state, "image": base_state["images"][0], "ssh_key_id": keys[0][0], "account_id":
                        await db.add_account(UID, "digitalocean", "me", "t", "")})
    seen = {}

    async def fake_run(client, job, progress):
        seen["key"], seen["name"] = job.ssh_key, job.ssh_key_name
        return job
    orig = buy.run_purchase
    buy.run_purchase = fake_run
    try:
        await dp.feed_update(bot, cb_update(8, "buy:go"))
        for _ in range(100):
            await ORIG_SLEEP(0.01)
            if seen:
                break
    finally:
        buy.run_purchase = orig
    assert seen == {"key": PUB, "name": "me@laptop"}

    # ---- Aeza: no key at order time → straight to the confirmation with a note ----
    await ctx.set_data({**base_state, "provider": "aeza"})
    await dp.feed_update(bot, cb_update(9, "buy:img:0"))
    assert "ключ при заказе передать нельзя" in s.texts()[-1] and "пароль root" in s.texts()[-1]

    # ---- deleting a saved key ----
    await ctx.set_data(base_state)
    await dp.feed_update(bot, cb_update(10, "buy:img:0"))
    await dp.feed_update(bot, cb_update(11, f"buy:auth:del:{keys[0][0]}"))
    assert await db.list_ssh_keys(UID) == []


async def providers_scenario():
    # DigitalOcean: key goes into user_data, no password is set
    digitalocean.asyncio.sleep = lambda x: ORIG_SLEEP(0)
    app, st = do_t.make_app([("142.93.99.1", "255.255.240.0")] * 3)
    do_keys = {"list": [], "posts": 0, "fail_post": False}

    async def account_keys(req):
        if req.method == "POST":
            do_keys["posts"] += 1
            if do_keys["fail_post"]:
                return web.json_response({"id": "forbidden", "message": "no scope"}, status=403)
            b = await req.json()
            do_keys["list"].append({"id": 501, "name": b["name"], "public_key": b["public_key"]})
            return web.json_response({"ssh_key": do_keys["list"][-1]}, status=201)
        return web.json_response({"ssh_keys": do_keys["list"], "links": {}})
    app.router.add_route("*", "/v2/account/keys", account_keys)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with DigitalOceanClient(do_t.TOKEN, str(srv.make_url("/v2"))) as c:
            job = PurchaseJob("fra1", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64", None,
                              ["142.93.96.0/20"], 1, max_attempts=0, parallel=1, active_timeout=30,
                              boot_volume_gb=10, ssh_key=PUB)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        ud = st["created"][0]["user_data"]
        assert PUB in ud and "/root/.ssh/authorized_keys" in ud and "chpasswd" not in ud
        assert 'chage -d "$(date +%F)" root' in ud, "lifts DigitalOcean's forced password change"
        assert st["created"][0]["ssh_keys"] == [501], "key passed through the API → DO sets no password"
        assert job.kept[0].admin_pass is None
        # a new client (next purchase): the key already in the account is found, not uploaded again
        async with DigitalOceanClient(do_t.TOKEN, str(srv.make_url("/v2"))) as c:
            await c.create_server("fra1", "n2", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64",
                                  None, 10, ssh_key=PUB)
        assert do_keys["posts"] == 1 and st["created"][1]["ssh_keys"] == [501]
        # uploading is refused (token scope) → still buys, key via script, expiry lifted
        do_keys["list"].clear()
        do_keys["fail_post"] = True
        async with DigitalOceanClient(do_t.TOKEN, str(srv.make_url("/v2"))) as c:
            await c.create_server("fra1", "n3", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64",
                                  None, 10, ssh_key=PUB)
        assert "ssh_keys" not in st["created"][2] and "chage -d" in st["created"][2]["user_data"]
    finally:
        await srv.close()
        digitalocean.asyncio.sleep = ORIG_SLEEP

    # Selectel: key uploaded once via /sshkeys and passed by id
    selectel.asyncio.sleep = lambda x: ORIG_SLEEP(0)
    selectel.CREATE_INTERVAL = 0
    app, st = sel_t.make_app(["31.184.2.20"])
    keys = []

    async def sshkeys(req):
        if req.method == "POST":
            b = await req.json()
            keys.append({"id": 77, "name": b["name"], "key": b["key"]})
            return web.json_response(keys[-1])
        return web.json_response(keys)
    app.router.add_route("*", "/v1/sshkeys", sshkeys)
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with SelectelVDSClient(sel_t.TOKEN, str(srv.make_url("/v1"))) as c:
            for _ in range(2):
                s1 = await c.create_server("spb0", "x", Flavor("small", "small", 1, 512, 20), "img", None, 10,
                                           ssh_key=PUB)
                assert s1.admin_pass is None
        assert len(keys) == 1, "the key is uploaded once and reused"
        assert [b.get("keys") for b in st["created"]] == [[77], [77]] and "password" not in st["created"][0]
    finally:
        await srv.close()
        selectel.asyncio.sleep = ORIG_SLEEP


def test_ssh_keys():
    asyncio.run(scenario())
    asyncio.run(providers_scenario())


if __name__ == "__main__":
    test_ssh_keys()
    print("OK")
