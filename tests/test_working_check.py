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
    app, st = do_t.make_app([("164.92.170.5", "255.255.240.0"), ("142.93.100.1", "255.255.240.0"),
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
            job = PurchaseJob("fra1", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64", None,
                              ["142.93.96.0/20"], 1,
                              max_attempts=0, parallel=1, active_timeout=30, boot_volume_gb=10,
                              check_url=config.check_url, bot_key=sshkey.bot_key(), check_timeout=20)

            async def on_found(s):
                found.append(s.ipv4[0])
            job.on_found = on_found
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert found == ["142.93.99.1"] and [k.ipv4[0] for k in job.kept] == ["142.93.99.1"]
        assert sorted(st["deleted"]) == [9000, 9001]
        assert job.checked == 2, "the wrong-IP server is deleted without a check, only subnet matches are checked"
        assert any("164.92.170.5</code> — не тот IP, удалён" in line for line in job.log)
        assert any("142.93.100.1</code> — не прошёл проверку, удалён" in line for line in job.log)
        assert any("проверка пройдена, сервер рабочий" in line for line in job.log)
        assert any("не прошёл проверку, удалён" in line for line in job.log)
        secret = ("turkmen", "ssh", "curl", "сайт", "страниц")
        assert not any(w in line.lower() for line in job.log for w in secret), job.log
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
    assert "Шаг 1.</b> Выберите провайдера" in s.texts()[-1], "no separate mode screen any more"

    acc = await db.add_account(UID, "selectel", "me", "t", "")
    await ctx.update_data(provider="selectel", account_id=acc, regions=["spb0"],
                          region_labels={"spb0": "🇷🇺 Санкт-Петербург · spb0"})
    object.__setattr__(config, "selectel_api_url", "http://127.0.0.1:9")  # ip_pools fails → still fine
    await dp.feed_update(bot, cb_update(3, "buy:reg:0"))
    assert "Шаг 3." in s.texts()[-1] and "🌐 Любой IP" in markup()
    await dp.feed_update(bot, cb_update(4, "buy:ipany"))
    count = s.texts()[-1]
    assert "Шаг 4.</b> Сколько серверов нужно" in count and "любой IP" in count and "Только рабочие" in count, count
    data = await ctx.get_data()
    assert data["prefixes"] == [] and data["network_id"] is None

    await ctx.update_data(count=2, flavor={"id": "small", "name": "small", "vcpus": 1, "ram_mb": 512, "disk_gb": 20,
                                           "price": ""}, has_netplan=False,
                          images=[{"id": "ubuntu", "name": "🟠 Ubuntu 22.04"}])
    await dp.feed_update(bot, cb_update(5, "buy:img:0"))
    assert "Шаг 7.</b> Как входить на сервер" in s.texts()[-1], s.texts()[-1]  # 1 … 3 IP, 4 кол-во, 5 тариф, 6 ОС
    await dp.feed_update(bot, cb_update(6, "buy:auth:pw"))
    confirm = s.texts()[-1]
    print("\n" + confirm)
    assert "🎯 Ищем IP: 🌐 <b>любой IP</b>" in confirm and "✅ Только рабочие" in confirm
    shown = "\n".join(s.texts()).lower()
    for word in ("turkmen", "curl", "сайт", "по ssh", "открывается"):
        assert word not in shown, f"the check method leaks into the bot texts: {word}"
    assert buy._step({"has_netplan": True}, "os") == 7 and buy._step({}, "count") == 4


async def ports_scenario():
    """HostVDS fast mode + check: a port IP whose server fails the check → the hunt goes on."""
    from bot.providers.hostvds import HostVDSClient
    from tests import test_purchase as hv_t
    app, st = hv_t.make_port_app([f"94.103.80.{i}" for i in (11, 12)] + [f"123.45.0.{i}" for i in range(10, 60)])
    srv = TestServer(app)
    await srv.start_server()
    calls = []

    async def fake_check(ip, url, password=None, key_file=None, ssh_timeout=300):
        calls.append(ip)
        return (ip == "94.103.80.12", "" if ip == "94.103.80.12" else "nope")
    orig = checker.check_server
    checker.check_server = fake_check
    try:
        async with HostVDSClient(str(srv.make_url("/identity")), "u", "p", "u") as c:
            job = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["94.103.80"], 1,
                              max_attempts=0, parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=1,
                              check_url=config.check_url)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert calls == ["94.103.80.11", "94.103.80.12"], calls
        assert [k.ipv4[0] for k in job.kept] == ["94.103.80.12"] and job.deleted == 1
    finally:
        checker.check_server = orig
        await srv.close()


async def learning_scenario(port):
    """Bad /24 blocks are learned: after 2 failed checks and no pass, servers there are dropped unchecked."""
    from bot.buyer import block_is_bad
    checker.target = lambda ip: (CURRENT_IP.__setitem__(0, ip) or ("127.0.0.1", port))
    digitalocean.asyncio.sleep = lambda s: ORIG_SLEEP(0)
    app, st = do_t.make_app([("142.93.100.1", "255.255.240.0"), ("142.93.100.2", "255.255.240.0"),
                             ("142.93.100.3", "255.255.240.0"), ("142.93.99.1", "255.255.240.0")])
    st["limit"] = 100
    app.router.add_route("*", "/v2/account/keys", lambda r: do_t.web.json_response(
        {"ssh_keys": [], "links": {}}) if r.method == "GET" else do_t.web.json_response({"ssh_key": {"id": 1}}, status=201))
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with DigitalOceanClient(do_t.TOKEN, str(srv.make_url("/v2"))) as c:
            job = PurchaseJob("fra1", Flavor("s-1vcpu-1gb", "s", 1, 1024, 25), "ubuntu-24-04-x64", None,
                              ["142.93.96.0/20"], 1, max_attempts=0, parallel=1, active_timeout=30,
                              boot_volume_gb=10, check_url=config.check_url, bot_key=sshkey.bot_key(),
                              check_timeout=20)
            await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
        print("\n".join(job.log))
        assert job.checked == 3, "142.93.100.1 and .2 checked and failed, .3 skipped, 142.93.99.1 checked and passed"
        assert job.skipped_bad == 1 and any("блок не проходит проверку" in l for l in job.log)
        assert [k.ipv4[0] for k in job.kept] == ["142.93.99.1"]
        assert job.block_stats["142.93.100"] == [0, 2] and job.block_stats["142.93.99"] == [1, 0]
        assert block_is_bad(job, "142.93.100.77") and not block_is_bad(job, "142.93.99.5")
        await db.add_check_results("digitalocean", "fra1", job.check_results)
        assert await db.check_stats("digitalocean", "fra1") == {"142.93.100": [0, 2], "142.93.99": [1, 0]}
    finally:
        await srv.close()
        digitalocean.asyncio.sleep = ORIG_SLEEP


async def ports_learning_scenario():
    """Fast mode: an IP from a known-bad block is not even bought."""
    from bot.providers.hostvds import HostVDSClient
    from tests import test_purchase as hv_t
    app, st = hv_t.make_port_app(["94.103.80.11", "94.103.81.5"] + [f"123.45.0.{i}" for i in range(10, 60)])
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with HostVDSClient(str(srv.make_url("/identity")), "u", "p", "u") as c:
            job = PurchaseJob("eu-west2", Flavor("f", "f", 1, 1024, 10), "img", "pub", ["94.103"], 1,
                              max_attempts=0, parallel=1, active_timeout=30, boot_volume_gb=10, port_parallel=1,
                              check_url=config.check_url, block_stats={"94.103.80": [0, 3]})
            orig = checker.check_server

            async def passes(ip, url, **kw):
                return True, ""
            checker.check_server = passes
            try:
                await run_purchase(c, job, lambda j: ORIG_SLEEP(0))
            finally:
                checker.check_server = orig
        assert [k.ipv4[0] for k in job.kept] == ["94.103.81.5"] and job.skipped_bad == 1
        assert len(st["servers"]) == 1, "no server was bought on the bad block's IP"
    finally:
        await srv.close()


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
    server2, port2, _ = await start_ssh(sshkey.bot_key()[1])
    try:
        await learning_scenario(port2)
    finally:
        server2.close()
    await ports_learning_scenario()
    await ports_scenario()
    await ui_scenario()
    await picker_scenario()


async def picker_scenario():
    """Learned results show up when picking subnets; the speed choice reaches the purchase."""
    s = FakeSession()
    bot = Bot("1:x", session=s)
    dp = Dispatcher()
    for r in (access.router, menu.router, accounts.router, servers.router, buy.router, update.router):
        r._parent_router = None
    dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
    ctx = dp.fsm.get_context(bot, UID, UID)
    from bot.providers import digitalocean as do_mod
    do_mod_geo = do_mod.config.do_geofeed_url
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/geo", lambda r: web.Response(text="142.93.96.0/20,DE,DE-HE,Frankfurt,\n164.92.160.0/20,DE,DE-HE,Frankfurt,\n"))
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "do_geofeed_url", str(srv.make_url("/geo")))
    from bot.providers import geofeed
    geofeed._CACHE.clear()
    do_mod._GEOFEED = None
    try:
        acc = await db.add_account(UID, "digitalocean", "me", "t", "")
        await ctx.set_data({"provider": "digitalocean", "account_id": acc, "regions": ["fra1"],
                            "region_labels": {"fra1": "🇩🇪 Франкфурт · fra1"}})
        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        groups = s.texts()[-1]
        assert "🟢 <code>142.93.#.#</code>" in groups and "🟢 рабочих 1" in groups, groups
        last = next(c for c in reversed(s.calls) if getattr(c, "reply_markup", None))
        assert "🟢 Выбрать рабочие" in [b.text for row in last.reply_markup.inline_keyboard for b in row]
        await dp.feed_update(bot, cb_update(2, "buy:ipwork"))
        data = await ctx.get_data()
        assert [data["pools"][i]["prefix"] for i in data["selected"]] == ["142.93.96.0/20"]
        await dp.feed_update(bot, cb_update(3, "buy:ipg:142.93"))
        assert "🟢 рабочих 1 · 🚫" not in s.texts()[-1] and "🟢 рабочих 1" in s.texts()[-1]
        # speed choice on the order summary
        await ctx.update_data(prefixes=["142.93.96.0/20"], count=1, image={"id": "u", "name": "Ubuntu"},
                              flavor={"id": "s", "name": "s", "vcpus": 1, "ram_mb": 1024, "disk_gb": 25, "price": ""})
        await dp.feed_update(bot, cb_update(4, "buy:par:10"))
        assert "⚡ <b>Скорость:</b> 10 серверов одновременно" in s.texts()[-1]
        assert (await ctx.get_data())["parallel"] == 10
    finally:
        await srv.close()
        object.__setattr__(config, "do_geofeed_url", do_mod_geo)


def test_working_check():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_working_check()
    print("OK")
