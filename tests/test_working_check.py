""""Only working" purchase: the bot logs in to each new server over SSH, opens the check page and keeps
only servers where the HTML comes back. A real SSH server (asyncssh) stands in for the VPS."""
import asyncio
import tempfile

import asyncssh
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import checker, db, sshkey  # noqa: E402
from bot.buyer import PurchaseJob, run_purchase  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import Flavor, digitalocean  # noqa: E402
from bot.providers.digitalocean import DigitalOceanClient  # noqa: E402
from tests import test_digitalocean as do_t  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402

ORIG_SLEEP = asyncio.sleep
HTML = "<!doctype html>\n<html lang='tm'><head><title>Turkmenistan Airlines</title></head></html>"
WORKING = {"142.93.99.1", "10.0.0.2"}        # IPs from which the site "opens"
PASSWORD = "Secret12345"


class VPS(asyncssh.SSHServer):
    def __init__(self, bot_pub: str):
        self.bot_pub = bot_pub

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "root" and password == PASSWORD

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        return username == "root" and key.export_public_key().decode().split()[1] == self.bot_pub.split()[1]


async def start_ssh(bot_pub: str):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    commands = []

    async def handle(process):
        commands.append((process.command, process.get_extra_info("vps_ip")))
        ip = process.channel.get_connection()._vps_ip if hasattr(process.channel.get_connection(), "_vps_ip") else ""
        if "curl" in (process.command or "") and CURRENT_IP[0] in WORKING:
            process.stdout.write(HTML)
        process.exit(0)
    server = await asyncssh.create_server(lambda: VPS(bot_pub), "127.0.0.1", 0, server_host_keys=[host_key],
                                          process_factory=handle)
    return server, server.sockets[0].getsockname()[1], commands


CURRENT_IP = [""]


async def checker_scenario(port):
    key_file, _ = sshkey.bot_key()
    checker.target = lambda ip: (CURRENT_IP.__setitem__(0, ip) or ("127.0.0.1", port))
    ok, why = await checker.check_server("142.93.99.1", config.check_url, key_file=key_file, ssh_timeout=20)
    assert (ok, why) == (True, ""), why
    ok, why = await checker.check_server("164.92.1.1", config.check_url, key_file=key_file, ssh_timeout=20)
    assert (ok, why) == (False, "страница не открылась")
    ok, why = await checker.check_server("10.0.0.2", config.check_url, password=PASSWORD, ssh_timeout=20)
    assert ok is True, "password login works too"
    orig = checker.asyncio.sleep
    checker.asyncio.sleep = lambda s: ORIG_SLEEP(0.05)
    try:
        ok, why = await checker.check_server("142.93.99.1", config.check_url, password="wrong", ssh_timeout=1)
        assert ok is None and why.startswith("SSH"), (ok, why)
    finally:
        checker.asyncio.sleep = orig


async def purchase_scenario(port):
    digitalocean.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    app, st = do_t.make_app([("164.92.170.5", "255.255.240.0"), ("165.22.1.9", "255.255.240.0"),
                             ("142.93.99.1", "255.255.240.0")])
    st["limit"] = 100
    keys = []

    async def account_keys(req):
        from aiohttp import web
        if req.method == "POST":
            b = await req.json()
            keys.append({"id": 600 + len(keys), "public_key": b["public_key"], "name": b["name"]})
            return web.json_response({"ssh_key": keys[-1]}, status=201)
        return web.json_response({"ssh_keys": keys, "links": {}})
    app.router.add_route("*", "/v2/account/keys", account_keys)
    srv = TestServer(app)
    await srv.start_server()
    found = []
    try:
        async with DigitalOceanClient(do_t.TOKEN, str(srv.make_url("/v2"))) as c:
            job = PurchaseJob("fra1", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64", None, [], 1,
                              max_attempts=0, parallel=1, active_timeout=30, boot_volume_gb=10,
                              check_url=config.check_url, bot_key=sshkey.bot_key(), check_timeout=20)

            async def on_found(s):
                found.append(s.ipv4[0])
            job.on_found = on_found
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert found == ["142.93.99.1"] and [k.ipv4[0] for k in job.kept] == ["142.93.99.1"]
        assert sorted(st["deleted"]) == [9000, 9001], "servers where the site did not open are deleted"
        assert job.checked == 3
        assert any("сайт открылся, сервер рабочий" in line for line in job.log)
        assert any("страница не открылась, удалён" in line for line in job.log)
        # the bot's key went to the droplet together with the password set-up
        body = st["created"][0]
        assert body["ssh_keys"] == [600] and keys[0]["public_key"] == sshkey.bot_key()[1]
        assert "chpasswd" in body["user_data"], "you chose a password: it is still set"
        assert job.kept[0].admin_pass and len(job.kept[0].admin_pass) == 16
    finally:
        await srv.close()
        digitalocean.asyncio.sleep = ORIG_SLEEP


async def ui_scenario():
    s = FakeSession()
    bot = Bot("1:x", session=s)
    dp = Dispatcher()
    dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
    ctx = dp.fsm.get_context(bot, UID, UID)

    def markup():
        last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
        return [b.text for row in last.reply_markup.inline_keyboard for b in row]

    await dp.feed_update(bot, cb_update(1, "buy"))
    assert "Как будем искать сервер" in s.texts()[-1] and "turkmenistanairlines.tm" in s.texts()[-1]
    assert markup()[:2] == ["🎯 По подсетям", "✅ Только рабочий"]
    await dp.feed_update(bot, cb_update(2, "buy:mode:check"))
    assert "✅ Только рабочий сервер" in s.texts()[-1] and "Шаг 1." in s.texts()[-1]
    assert (await ctx.get_data())["buy_mode"] == "check"

    # region → straight to the count (no subnet step), numbering continues 1, 2, 3 …
    acc = await db.add_account(UID, "selectel", "me", "t", "")
    await ctx.update_data(provider="selectel", account_id=acc, regions=["spb0"],
                          region_labels={"spb0": "🇷🇺 Санкт-Петербург · spb0"})
    object.__setattr__(config, "selectel_api_url", "http://127.0.0.1:9")  # ip_pools fails → still fine
    await dp.feed_update(bot, cb_update(3, "buy:reg:0"))
    count = s.texts()[-1]
    assert "Шаг 3.</b> Сколько рабочих серверов нужно" in count, count
    data = await ctx.get_data()
    assert data["prefixes"] == [] and data["network_id"] is None

    await ctx.update_data(count=2, flavor={"id": "small", "name": "small", "vcpus": 1, "ram_mb": 512, "disk_gb": 20,
                                           "price": ""}, has_netplan=False,
                          images=[{"id": "ubuntu", "name": "🟠 Ubuntu 22.04"}])
    await dp.feed_update(bot, cb_update(4, "buy:img:0"))
    assert "Шаг 6.</b> Как входить на сервер" in s.texts()[-1], s.texts()[-1]  # 1 провайдер … 5 ОС, 6 доступ
    await dp.feed_update(bot, cb_update(5, "buy:auth:pw"))
    confirm = s.texts()[-1]
    print("\n" + confirm)
    assert "✅ Только рабочий: открывается <code>turkmenistanairlines.tm</code>" in confirm
    assert "Ищем IP" not in confirm and "заходит на каждый новый сервер по SSH" in confirm

    # subnet mode keeps the old numbering with the IP step
    await dp.feed_update(bot, cb_update(6, "buy:mode:subnets"))
    data = await ctx.get_data()
    assert buy._step(data, "count") == 4 and buy._step({**data, "has_netplan": True}, "os") == 7
    assert buy._step({"buy_mode": "check"}, "os") == 5


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    _, pub = sshkey.bot_key()
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" vps-buyer-bot")
    assert sshkey.bot_key()[1] == pub, "the key is created once and reused"
    server, port, _ = await start_ssh(pub)
    try:
        await checker_scenario(port)
        await purchase_scenario(port)
    finally:
        server.close()
    await ui_scenario()


def test_working_check():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_working_check()
    print("OK")
