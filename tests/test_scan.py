"""Selectel IP scouting through the bot: offer with cost → scan → report → blocks appear in the IP step."""
import asyncio
import tempfile

from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestServer

from bot.config import config

object.__setattr__(config, "hostvds_public_api", "http://127.0.0.1:9")

from bot import db  # noqa: E402
from bot.handlers import access, accounts, buy, menu, servers, update  # noqa: E402
from bot.providers import selectel  # noqa: E402
from tests.test_selectel import TOKEN, make_app  # noqa: E402
from tests.test_servers_update import UID, FakeSession, cb_update  # noqa: E402


async def scenario():
    object.__setattr__(config, "db_path", tempfile.mkdtemp() + "/t.db")
    object.__setattr__(config, "admin_ids", {UID})
    await db.init_db()
    orig_sleep = asyncio.sleep
    selectel.asyncio.sleep = lambda s: orig_sleep(0.01)
    selectel.CREATE_INTERVAL = 0
    app, st = make_app(["31.184.2.20", "5.188.1.10", "31.184.2.21", "31.184.2.22", "5.188.1.11"])
    srv = TestServer(app)
    await srv.start_server()
    object.__setattr__(config, "selectel_api_url", str(srv.make_url("/v1")))
    try:
        acc = await db.add_account(UID, "selectel", "me@example.com", TOKEN, "")
        s = FakeSession()
        bot = Bot("1:x", session=s)
        dp = Dispatcher()
        dp.include_routers(access.router, menu.router, accounts.router, servers.router, buy.router, update.router)
        ctx = dp.fsm.get_context(bot, UID, UID)
        await ctx.set_data({"provider": "selectel", "account_id": acc, "regions": ["spb0"],
                            "region_labels": {"spb0": "🇷🇺 Санкт-Петербург · spb0"}})

        await dp.feed_update(bot, cb_update(1, "buy:reg:0"))
        ip_buttons = [b.text for row in s.calls[-1].reply_markup.inline_keyboard for b in row]
        assert "🔍 Разведать доступные IP" in ip_buttons, ip_buttons
        await dp.feed_update(bot, cb_update(2, "buy:scan"))
        offer = s.texts()[-1]
        buttons = [b.text for row in s.calls[-1].reply_markup.inline_keyboard for b in row]
        print(offer, buttons, sep="\n")
        assert "Разведка IP" in offer and "0.29 ₽/час" in offer
        assert buttons[:3] == ["🔍 5 серверов · ≈1.45 ₽", "🔍 10 серверов · ≈2.90 ₽", "🔍 20 серверов · ≈5.80 ₽"]

        await dp.feed_update(bot, cb_update(3, "buy:scango:5"))
        for _ in range(500):
            await orig_sleep(0.02)
            if any("Отчёт разведки" in t for t in s.texts()):
                break
        report = next(t for t in s.texts() if "Отчёт разведки" in t)
        print("\n" + report)
        assert "Проверено серверов: <b>5</b> · получено IP: <b>5</b>" in report
        assert "<code>31.184.2.#</code> — 3 из 5 (60%)" in report and "<code>5.188.1.#</code> — 2 из 5 (40%)" in report
        assert len(st["deleted"]) == 5, "every probe server is deleted"
        assert buy.JOBS == {}

        # the blocks now show up in the IP step
        await dp.feed_update(bot, cb_update(4, "buy:reg:0"))
        ip_screen = s.texts()[-1]
        assert "<code>31.184.2.#</code>" in ip_screen and "<code>5.188.1.#</code>" in ip_screen
    finally:
        await srv.close()
        selectel.asyncio.sleep = orig_sleep


def test_scan():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_scan()
    print("OK")
