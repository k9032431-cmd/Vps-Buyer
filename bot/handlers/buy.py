import asyncio
import logging
import time
from dataclasses import asdict

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db, geo
from ..buyer import PurchaseJob, run_purchase
from ..config import config
from ..providers import Flavor, IpPool, ProviderError
from ..keyboards import PROVIDERS, back, grid, kb
from .accounts import accounts_view, make_client

router = Router()
log = logging.getLogger(__name__)

# user_id -> running job
JOBS: dict[int, PurchaseJob] = {}

POPULAR_OS = ("ubuntu", "debian", "centos", "alma", "rocky", "windows")


class Buy(StatesGroup):
    custom_prefix = State()
    count = State()


async def _client_for(cb_or_msg, state: FSMContext):
    data = await state.get_data()
    acc = await db.get_account(cb_or_msg.from_user.id, data["account_id"])
    if not acc:
        raise ProviderError("Аккаунт не найден")
    return make_client(acc)


async def _error(cb: CallbackQuery, e: Exception):
    await cb.message.edit_text(f"❌ Ошибка: {hd.quote(str(e))}", reply_markup=kb(back("buy")))


# ---------- 1. provider ----------
@router.callback_query(F.data == "buy")
async def choose_provider(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if cb.from_user.id in JOBS:
        await cb.answer("У вас уже идёт покупка — дождитесь окончания или отмените её.", show_alert=True)
        return
    rows = [[Btn(text=f"🌐 {name}", callback_data=f"buy:prov:{key}")] for key, name in PROVIDERS.items()]
    await cb.message.edit_text("🛒 <b>Купить сервер</b>\n\nВыберите провайдера:", reply_markup=kb(*rows, back()))
    await cb.answer()


# ---------- 2. account ----------
@router.callback_query(F.data.startswith("buy:prov:"))
async def choose_account(cb: CallbackQuery, state: FSMContext):
    provider = cb.data.split(":")[2]
    await state.update_data(provider=provider)
    text, markup = await accounts_view(cb.from_user.id, provider, select_prefix="buy:acc:")
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


# ---------- 3. location ----------
# account_id -> (timestamp, [(region, label, available, reason)])
_REGION_CACHE: dict[int, tuple[float, list[tuple[str, str, bool, str]]]] = {}
REGION_CACHE_TTL = 300


async def _load_regions(cb: CallbackQuery, state: FSMContext, provider: str) -> list[tuple[str, str, bool, str]]:
    account_id = (await state.get_data())["account_id"]
    cached = _REGION_CACHE.get(account_id)
    if cached and time.time() - cached[0] < REGION_CACHE_TTL:
        return cached[1]
    async with await _client_for(cb, state) as client:
        statuses = await client.region_statuses()
    locations = await asyncio.gather(*(geo.locate(provider, s.region, s.sample_ip, s.country, s.city)
                                       for s in statuses))
    result = [(s.region, f"{loc.flag} {loc.title} · {s.region}", s.available, s.reason)
              for s, loc in zip(statuses, locations)]
    _REGION_CACHE[account_id] = (time.time(), result)
    return result


@router.callback_query(F.data.startswith("buy:acc:"))
async def choose_region(cb: CallbackQuery, state: FSMContext):
    await show_regions(cb, state, int(cb.data.split(":")[2]))


async def show_regions(cb: CallbackQuery, state: FSMContext, account_id: int):
    await state.update_data(account_id=account_id)
    await cb.message.edit_text("⏳ Проверяю, в каких локациях можно купить сервер…")
    await cb.answer()
    data = await state.get_data()
    try:
        regions = await _load_regions(cb, state, data.get("provider", "hostvds"))
    except Exception as e:
        return await _error(cb, e)
    available = [(r, label) for r, label, ok, _ in regions if ok]
    unavailable = [(label, reason) for _, label, ok, reason in regions if not ok]
    if not available:
        text = "😔 Сейчас нет доступных локаций.\n\n" + "\n".join(
            f"• {hd.quote(label)} — {hd.quote(reason)}" for label, reason in unavailable)
        await cb.message.edit_text(text[:4000], reply_markup=kb(
            [Btn(text="🔄 Проверить снова", callback_data=f"buy:refresh:{data['account_id']}")], back("buy")))
        return
    await state.update_data(regions=[r for r, _ in available], region_labels={r: label for r, label in available})
    text = f"📍 <b>Выберите локацию</b>\n\nДоступно: {len(available)} из {len(regions)}"
    if unavailable:
        text += "\n\n<i>Недоступны:</i>\n" + "\n".join(
            f"• {hd.quote(label)} — {hd.quote(reason[:60])}" for label, reason in unavailable)
    buttons = [Btn(text=label, callback_data=f"buy:reg:{i}") for i, (_, label) in enumerate(available)]
    await cb.message.edit_text(text[:4000], reply_markup=kb(
        *grid(buttons, 1),
        [Btn(text="🔄 Обновить", callback_data=f"buy:refresh:{data['account_id']}")],
        back("buy")))


@router.callback_query(F.data.startswith("buy:refresh:"))
async def refresh_regions(cb: CallbackQuery, state: FSMContext):
    account_id = int(cb.data.split(":")[2])
    _REGION_CACHE.pop(account_id, None)
    await show_regions(cb, state, account_id)


# ---------- 4. IP pool ----------
@router.callback_query(F.data.startswith("buy:reg:"))
async def choose_ip(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    region = data["regions"][int(cb.data.split(":")[2])]
    label = data["region_labels"][region]
    await state.update_data(region=region, region_label=label)
    await cb.message.edit_text(f"⏳ Проверяю, какие IP есть в <b>{hd.quote(label)}</b>…")
    await cb.answer()
    try:
        async with await _client_for(cb, state) as client:
            pools = await client.ip_pools(region)
    except Exception as e:
        log.warning("ip_pools failed: %s", e)
        pools = []
    pools = _merge_seen(pools, await db.seen_ips(data["provider"], region))
    await state.update_data(pools=[asdict(p) for p in pools])
    lines = [f"• <b>{p.prefix}.x.x.x</b> — {', '.join(p.cidrs[:4])}{' …' if len(p.cidrs) > 4 else ''}"
             for p in pools]
    text = f"🌐 <b>IP-подсети в {hd.quote(label)}</b>\n\n"
    text += "\n".join(lines) if lines else (
        "Список подсетей пока неизвестен — бот запоминает IP, которые выдаёт провайдер, "
        "и после первой покупки покажет их здесь. Сейчас укажите префикс вручную.")
    text += "\n\nВыберите, какой IP нужен:"
    buttons = [Btn(text=f"{p.prefix}.x.x.x", callback_data=f"buy:ip:{i}") for i, p in enumerate(pools)]
    rows = grid(buttons, 3) + [[Btn(text="✏️ Свой префикс (напр. 94.103)", callback_data="buy:ip:custom")],
                               back(f"buy:acc:{data['account_id']}")]
    await cb.message.edit_text(text, reply_markup=kb(*rows))


def _merge_seen(pools: list[IpPool], seen: list[str]) -> list[IpPool]:
    """Adds prefixes of IPs the provider gave out before (for providers without a subnet API)."""
    known = {p.prefix: p for p in pools}
    for ip in seen:
        prefix = ip.split(".")[0]
        if prefix not in known:
            known[prefix] = IpPool(prefix, None, [])
        pool = known[prefix]
        if pool.network_id is None and len(pool.cidrs) < 5 and ip not in pool.cidrs:
            pool.cidrs.append(ip)
    return sorted(known.values(), key=lambda p: int(p.prefix))


@router.callback_query(F.data == "buy:ip:custom")
async def custom_prefix(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Buy.custom_prefix)
    await cb.message.edit_text("✏️ Отправьте начало IP, например <code>94</code> или <code>94.103</code>:",
                               reply_markup=kb(back("buy", "❌ Отмена")))
    await cb.answer()


@router.message(Buy.custom_prefix)
async def custom_prefix_set(message: Message, state: FSMContext):
    prefix = message.text.strip().rstrip(".")
    parts = prefix.split(".")
    if not (1 <= len(parts) <= 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)):
        await message.answer("Неверный формат. Пример: <code>94</code> или <code>94.103</code>")
        return
    data = await state.get_data()
    # Use the pool network if the prefix belongs to a known pool, otherwise let the cloud pick.
    pool = next((p for p in data.get("pools", []) if p["prefix"] == parts[0]), None)
    await state.update_data(prefix=prefix, network_id=pool["network_id"] if pool else None)
    await ask_count(message, state)


@router.callback_query(F.data.startswith("buy:ip:"))
async def ip_selected(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pool = data["pools"][int(cb.data.split(":")[2])]
    await state.update_data(prefix=pool["prefix"], network_id=pool["network_id"])
    await cb.answer()
    await ask_count(cb.message, state, edit=True)


# ---------- 5. count ----------
async def ask_count(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(Buy.count)
    data = await state.get_data()
    text = (f"🔢 Нужен IP <b>{data['prefix']}.x</b>\n\nСколько серверов купить? "
            "Выберите или отправьте число:")
    buttons = [Btn(text=str(n), callback_data=f"buy:cnt:{n}") for n in (1, 2, 3, 5, 10)]
    markup = kb(buttons, back("buy", "❌ Отмена"))
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Buy.count)
async def count_text(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 100:
        await message.answer("Отправьте число от 1 до 100.")
        return
    await state.update_data(count=int(message.text))
    msg = await message.answer("⏳ Загружаю тарифы…")
    await show_flavors(msg, state, message.from_user.id)


@router.callback_query(F.data.startswith("buy:cnt:"))
async def count_btn(cb: CallbackQuery, state: FSMContext):
    await state.update_data(count=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.edit_text("⏳ Загружаю тарифы…")
    await show_flavors(cb.message, state, cb.from_user.id)


# ---------- 6. flavor ----------
async def show_flavors(msg: Message, state: FSMContext, user_id: int):
    await state.set_state(None)
    data = await state.get_data()
    try:
        acc = await db.get_account(user_id, data["account_id"])
        async with make_client(acc) as client:
            flavors = await client.list_flavors(data["region"])
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {hd.quote(str(e))}", reply_markup=kb(back("buy")))
        return
    flavors = flavors[:30]
    await state.update_data(flavors=[asdict(f) for f in flavors])
    buttons = [Btn(text=f"{f.name} · {f.vcpus}vCPU/{f.ram_mb / 1024:g}GB"
                        + (f"/{f.disk_gb}GB" if f.disk_gb else "") + (f" · {f.price}" if f.price else ""),
                   callback_data=f"buy:fl:{i}")
               for i, f in enumerate(flavors)]
    await msg.edit_text("⚙️ <b>Выберите тариф:</b>", reply_markup=kb(*grid(buttons, 1), back("buy", "❌ Отмена")))


# ---------- 7. image ----------
@router.callback_query(F.data.startswith("buy:fl:"))
async def choose_image(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    flavor = data["flavors"][int(cb.data.split(":")[2])]
    await state.update_data(flavor=flavor)
    await cb.message.edit_text("⏳ Загружаю образы ОС…")
    await cb.answer()
    try:
        async with await _client_for(cb, state) as client:
            images = await client.list_images(data["region"], Flavor(**flavor))
    except Exception as e:
        return await _error(cb, e)

    def rank(img):
        name = img.name.lower()
        idx = next((i for i, os_ in enumerate(POPULAR_OS) if os_ in name), len(POPULAR_OS))
        return idx, name

    images = sorted(images, key=rank)[:24]
    await state.update_data(images=[asdict(i) for i in images])
    buttons = [Btn(text=i.name[:40], callback_data=f"buy:img:{n}") for n, i in enumerate(images)]
    await cb.message.edit_text("💿 <b>Выберите ОС:</b>", reply_markup=kb(*grid(buttons), back("buy", "❌ Отмена")))


# ---------- 8. confirm ----------
@router.callback_query(F.data.startswith("buy:img:"))
async def confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    image = data["images"][int(cb.data.split(":")[2])]
    await state.update_data(image=image)
    f = data["flavor"]
    text = (
        "🧾 <b>Подтверждение</b>\n\n"
        f"Провайдер: <b>{PROVIDERS[data['provider']]}</b>\n"
        f"Локация: <b>{hd.quote(data['region_label'])}</b>\n"
        f"Нужный IP: <b>{data['prefix']}.x</b>\n"
        f"Тариф: <b>{hd.quote(f['name'])}</b> ({f['vcpus']} vCPU, {f['ram_mb']} MB"
        f"{', ' + f['price'] if f.get('price') else ''})\n"
        f"ОС: <b>{hd.quote(image['name'])}</b>\n"
        f"Количество: <b>{data['count']}</b>\n\n"
        f"⚠️ Серверы с чужим IP будут удаляться и создаваться заново "
        f"(максимум {config.max_attempts} созданий за задачу). Каждое создание может стоить денег."
    )
    await cb.message.edit_text(text, reply_markup=kb(
        [Btn(text="✅ Купить", callback_data="buy:go")], back("buy", "❌ Отмена")))
    await cb.answer()


# ---------- 9. run ----------
def _progress_text(job: PurchaseJob, done: bool = False) -> str:
    head = "🏁 <b>Готово</b>" if done else "⏳ <b>Покупка идёт…</b>"
    text = (f"{head}\n\nНужный IP: <b>{job.prefix}.x</b> в {hd.quote(job.region)}\n"
            f"Найдено: <b>{len(job.kept)}/{job.count}</b> · создано: {job.attempts} · удалено: {job.deleted}\n\n")
    text += "\n".join(hd.quote(line) for line in job.log[-12:])
    return text[:4000]


@router.callback_query(F.data == "buy:go")
async def go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    if uid in JOBS:
        await cb.answer("Покупка уже идёт", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account(uid, data["account_id"])
    job = PurchaseJob(
        region=data["region"], flavor=Flavor(**data["flavor"]), image_id=data["image"]["id"],
        network_id=data.get("network_id"), prefix=data["prefix"], count=data["count"],
        max_attempts=config.max_attempts, parallel=config.parallel,
        active_timeout=config.active_timeout, boot_volume_gb=config.boot_volume_gb,
    )
    JOBS[uid] = job
    cancel_kb = kb([Btn(text="⛔ Остановить", callback_data="buy:cancel")])
    await cb.message.edit_text(_progress_text(job), reply_markup=cancel_kb)
    await cb.answer()
    asyncio.create_task(_run(bot, cb.message, uid, acc, job, cancel_kb))


async def _run(bot: Bot, msg: Message, uid: int, acc: db.Account, job: PurchaseJob, cancel_kb):
    last_edit = 0.0

    async def progress(j: PurchaseJob):
        nonlocal last_edit
        if time.time() - last_edit < 3:
            return
        last_edit = time.time()
        try:
            await msg.edit_text(_progress_text(j), reply_markup=cancel_kb)
        except Exception:
            pass

    try:
        async with make_client(acc) as client:
            await run_purchase(client, job, progress)
    except Exception as e:
        log.exception("purchase failed")
        job.error = str(e)
    finally:
        JOBS.pop(uid, None)

    await db.add_seen_ips(acc.provider, job.region, job.seen)
    for s in job.kept:
        await db.add_server(uid, acc.id, job.region, s.id, s.name, s.ipv4[0], s.admin_pass)

    try:
        await msg.edit_text(_progress_text(job, done=True))
    except Exception:
        pass

    if job.kept:
        lines = [f"🖥 <code>{s.ipv4[0]}</code> — {hd.quote(s.name)}"
                 + (f"\n    пароль root: <tg-spoiler>{hd.quote(s.admin_pass)}</tg-spoiler>" if s.admin_pass else "")
                 for s in job.kept]
        await bot.send_message(uid, "✅ <b>Ваши серверы:</b>\n\n" + "\n".join(lines))
    reason = ("остановлено вами" if job.cancelled else
              f"ошибка: {hd.quote(job.error)}" if job.error else
              "достигнут лимит попыток" if len(job.kept) < job.count else None)
    if reason:
        await bot.send_message(uid, f"⚠️ Куплено {len(job.kept)} из {job.count} — {reason}.")
    await bot.send_message(uid, "Что дальше?", reply_markup=kb(back("menu", "🏠 Главное меню")))


@router.callback_query(F.data == "buy:cancel")
async def cancel(cb: CallbackQuery):
    job = JOBS.get(cb.from_user.id)
    if job:
        job.cancelled = True
        await cb.answer("Останавливаю после текущей попытки…", show_alert=True)
    else:
        await cb.answer("Нет активной покупки")
