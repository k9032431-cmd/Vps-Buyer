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
from .. import os_names
from ..providers import Flavor, IpPool, ProviderError
from ..providers.base import prefix24
from ..keyboards import PROVIDERS, back, grid, kb
from .accounts import accounts_view, make_client

router = Router()
log = logging.getLogger(__name__)

# user_id -> running job
JOBS: dict[int, PurchaseJob] = {}

MAX_IP_BUTTONS = 60


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
    await state.update_data(pools=[asdict(p) for p in pools], selected=[], custom=[])
    await render_ip_screen(cb.message, state, edit=True)


async def render_ip_screen(msg: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    pools, selected, custom = data["pools"], set(data.get("selected", [])), data.get("custom", [])
    shown = pools[:MAX_IP_BUTTONS]
    text = f"🌐 <b>Доступные IP в {hd.quote(data['region_label'])}</b> ({len(pools)})\n\n"
    if pools:
        text += "\n".join(f"{'✅' if i in selected else '▫️'} <code>{p['prefix']}.#</code>"
                          for i, p in enumerate(pools[:150]))
        if len(pools) > 150:
            text += f"\n… и ещё {len(pools) - 150}"
        if len(pools) > len(shown):
            text += f"\n\nКнопками показаны первые {len(shown)} — остальные добавьте через «✏️ Свой IP»."
    else:
        text += ("Список подсетей пока неизвестен — бот запоминает IP, которые выдаёт провайдер, "
                 "и после первой покупки покажет их здесь. Сейчас добавьте IP вручную.")
    if custom:
        text += "\n\n✏️ Добавлено вручную: " + ", ".join(f"<code>{show_prefix(c)}</code>" for c in custom)
    total = len(selected) + len(custom)
    text += ("\n\n<b>Отметьте один или несколько IP</b> — бот купит сервер, как только выпадет любой из них."
             f"\nВыбрано: <b>{total}</b>")
    buttons = [Btn(text=f"{'✅ ' if i in selected else ''}{p['prefix']}.#", callback_data=f"buy:ipt:{i}")
               for i, p in enumerate(shown)]
    rows = grid(buttons, 2)
    if len(shown) > 1:
        rows.append([Btn(text="☑️ Выбрать все", callback_data="buy:ipall"),
                     Btn(text="🔄 Сбросить", callback_data="buy:ipclr")])
    rows.append([Btn(text="✏️ Свой IP (напр. 95.182.87)", callback_data="buy:ip:custom")])
    if total:
        rows.append([Btn(text=f"➡️ Готово ({total})", callback_data="buy:ipok")])
    rows.append(back(f"buy:acc:{data['account_id']}"))
    markup = kb(*rows)
    if edit:
        await msg.edit_text(text[:4000], reply_markup=markup)
    else:
        await msg.answer(text[:4000], reply_markup=markup)


@router.callback_query(F.data.startswith("buy:ipt:"))
async def ip_toggle(cb: CallbackQuery, state: FSMContext):
    i = int(cb.data.split(":")[2])
    selected = set((await state.get_data()).get("selected", []))
    selected ^= {i}
    await state.update_data(selected=sorted(selected))
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(F.data.in_({"buy:ipall", "buy:ipclr"}))
async def ip_all_or_clear(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if cb.data == "buy:ipall":
        await state.update_data(selected=list(range(min(len(data["pools"]), MAX_IP_BUTTONS))))
    else:
        await state.update_data(selected=[], custom=[])
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


def _merge_seen(pools: list[IpPool], seen: list[str]) -> list[IpPool]:
    """Adds /24 blocks of IPs the provider gave out before (for providers without a subnet API)."""
    known = {p.prefix: p for p in pools}
    for ip in seen:
        prefix = prefix24(ip)
        if prefix not in known:
            known[prefix] = IpPool(prefix, None, [])
        pool = known[prefix]
        if pool.network_id is None and len(pool.cidrs) < 5 and ip not in pool.cidrs:
            pool.cidrs.append(ip)
    return sorted(known.values(), key=lambda p: tuple(int(x) for x in p.prefix.split(".")))


def show_prefix(prefix: str) -> str:
    """"95.182.87" → "95.182.87.#", "94" → "94.#.#.#"."""
    parts = prefix.split(".")
    return ".".join(parts + ["#"] * (4 - len(parts))) if len(parts) < 4 else prefix


@router.callback_query(F.data == "buy:ip:custom")
async def custom_prefix(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Buy.custom_prefix)
    await cb.message.edit_text("✏️ Отправьте начало IP, например <code>95.182.87</code>, "
                               "<code>94.103</code> или <code>94</code>.\n"
                               "Можно несколько через пробел или запятую: <code>95.182.87 94.103.80</code>",
                               reply_markup=kb(back("buy:ipback", "⬅️ Назад к списку")))
    await cb.answer()


@router.callback_query(F.data == "buy:ipback")
async def ip_back(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


def _parse_prefix(raw: str) -> str | None:
    prefix = raw.strip().rstrip(".#").rstrip(".")
    parts = prefix.split(".")
    if 1 <= len(parts) <= 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return prefix
    return None


@router.message(Buy.custom_prefix)
async def custom_prefix_set(message: Message, state: FSMContext):
    raw = [x for x in message.text.replace(",", " ").replace(";", " ").split() if x]
    parsed = [_parse_prefix(x) for x in raw]
    if not raw or None in parsed:
        await message.answer("Неверный формат. Пример: <code>95.182.87</code> или <code>94 95.182</code>")
        return
    data = await state.get_data()
    pool_index = {p["prefix"]: i for i, p in enumerate(data["pools"])}
    selected, custom = set(data.get("selected", [])), list(data.get("custom", []))
    for prefix in parsed:
        if prefix in pool_index and pool_index[prefix] < MAX_IP_BUTTONS:
            selected.add(pool_index[prefix])        # it's a listed block → just tick it
        elif prefix not in custom:
            custom.append(prefix)
    await state.update_data(selected=sorted(selected), custom=custom)
    await state.set_state(None)
    await render_ip_screen(message, state, edit=False)


def _targets(pools: list[dict], prefixes: list[str]) -> list[list]:
    """For each wanted prefix: [network_id, subnet_id] to allocate from (None = let the cloud pick)."""
    result = []
    for prefix in prefixes:
        exact = next((p for p in pools if p["prefix"] == prefix), None)
        inside = next((p for p in pools if p["network_id"] and (p["prefix"] + ".").startswith(prefix + ".")), None)
        if exact and exact["network_id"]:
            result.append([exact["network_id"], exact.get("subnet_id")])
        elif inside:
            result.append([inside["network_id"], None])
        else:
            result.append([None, None])
    return result


@router.callback_query(F.data == "buy:ipok")
async def ip_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    prefixes = [data["pools"][i]["prefix"] for i in data.get("selected", [])] + data.get("custom", [])
    if not prefixes:
        return await cb.answer("Выберите хотя бы один IP", show_alert=True)
    targets = _targets(data["pools"], prefixes)
    network_id = next((t[0] for t in targets if t[0]), None)
    await state.update_data(prefixes=prefixes, targets=targets, network_id=network_id)
    await cb.answer()
    await ask_count(cb.message, state, edit=True)


def prefixes_text(prefixes: list[str], limit: int = 20) -> str:
    text = ", ".join(f"<code>{show_prefix(p)}</code>" for p in prefixes[:limit])
    return text + (f" и ещё {len(prefixes) - limit}" if len(prefixes) > limit else "")


# ---------- 5. count ----------
async def ask_count(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(Buy.count)
    data = await state.get_data()
    text = (f"🔢 Ищем IP: {prefixes_text(data['prefixes'])}\n\nСколько серверов купить (всего)? "
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

    choices, other = os_names.simplify(images)
    await state.update_data(images=[{"id": c.id, "name": c.title} for c in choices],
                            other_images=[asdict(i) for i in other[:40]])
    await _show_os(cb.message, choices, bool(other))


async def _show_os(msg: Message, choices, has_other: bool):
    buttons = [Btn(text=c.title, callback_data=f"buy:img:{n}") for n, c in enumerate(choices[:40])]
    rows = grid(buttons, 2)
    if has_other:
        rows.append([Btn(text="📦 Другие образы", callback_data="buy:imgother")])
    text = "💿 <b>Выберите операционную систему:</b>" if choices else "💿 <b>Выберите образ:</b>"
    await msg.edit_text(text, reply_markup=kb(*rows, back("buy", "❌ Отмена")))


@router.callback_query(F.data == "buy:imgother")
async def other_images(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = [Btn(text=i["name"][:45], callback_data=f"buy:imgo:{n}") for n, i in enumerate(data["other_images"])]
    await cb.message.edit_text("📦 <b>Другие образы</b> (приложения, панели, старые версии):",
                               reply_markup=kb(*grid(buttons, 1), [Btn(text="⬅️ К списку ОС", callback_data="buy:imgback")]))
    await cb.answer()


@router.callback_query(F.data == "buy:imgback")
async def images_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    choices = [os_names.OsChoice(i["id"], i["name"], i["name"]) for i in data["images"]]
    await _show_os(cb.message, choices, bool(data.get("other_images")))
    await cb.answer()


# ---------- 8. confirm ----------
@router.callback_query(F.data.startswith("buy:img:") | F.data.startswith("buy:imgo:"))
async def confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kind, idx = cb.data.split(":")[1:3]
    image = (data["other_images"] if kind == "imgo" else data["images"])[int(idx)]
    await state.update_data(image=image)
    f = data["flavor"]
    text = (
        "🧾 <b>Подтверждение</b>\n\n"
        f"Провайдер: <b>{PROVIDERS[data['provider']]}</b>\n"
        f"Локация: <b>{hd.quote(data['region_label'])}</b>\n"
        f"Нужные IP: {prefixes_text(data['prefixes'])}\n"
        f"Тариф: <b>{hd.quote(f['name'])}</b> ({f['vcpus']} vCPU, {f['ram_mb']} MB"
        f"{', ' + f['price'] if f.get('price') else ''})\n"
        f"ОС: <b>{hd.quote(image['name'])}</b>\n"
        f"Количество: <b>{data['count']}</b>\n\n"
        + ("⚡ Быстрый режим: IP подбирается бесплатно через сетевые порты, сервер покупается "
           "только на подходящий IP.\n" if data.get("network_id") and data["provider"] == "hostvds" else "")
        + f"⚠️ Если быстрый режим недоступен — серверы с чужим IP удаляются сразу, как только видно IP "
          f"(максимум {config.max_attempts} созданий). Каждое создание может стоить денег."
    )
    await cb.message.edit_text(text, reply_markup=kb(
        [Btn(text="✅ Купить", callback_data="buy:go")], back("buy", "❌ Отмена")))
    await cb.answer()


# ---------- 9. run ----------
def _progress_text(job: PurchaseJob, done: bool = False) -> str:
    head = "🏁 <b>Готово</b>" if done else "⏳ <b>Покупка идёт…</b>"
    text = f"{head}\n\nИщем IP: {prefixes_text(job.prefixes, 10)} в {hd.quote(job.region)}\n"
    if job.mode == "ports":
        text += f"⚡ Проверено IP (бесплатно): <b>{job.ports_checked}</b>\n"
    text += (f"Готово серверов: <b>{len(job.kept)}/{job.count}</b> · создано: {job.attempts} · "
             f"удалено: {job.deleted}\n\n")
    text += "\n".join(job.log[-12:])
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
        network_id=data.get("network_id"), prefixes=data["prefixes"], count=data["count"],
        targets=[tuple(t) for t in data.get("targets", [])], max_port_attempts=config.max_port_attempts,
        port_parallel=config.port_parallel,
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

    async def found(server):
        # Save and send each server right away — no need to wait for the whole job.
        ip = server.ipv4[0]
        await db.add_server(uid, acc.id, job.region, server.id, server.name, ip, server.admin_pass)
        text = (f"🎉 <b>Найден IP</b> <code>{ip}</code>\n\n"
                f"Сервер: {hd.quote(server.name)} · {hd.quote(job.region)}\n"
                f"Готово: {len(job.kept)}/{job.count}")
        if server.admin_pass:
            text += f"\nПароль root: <tg-spoiler>{hd.quote(server.admin_pass)}</tg-spoiler>"
        await bot.send_message(uid, text)

    job.on_found = found
    try:
        async with make_client(acc) as client:
            await run_purchase(client, job, progress)
    except Exception as e:
        log.exception("purchase failed")
        job.error = str(e)
    finally:
        JOBS.pop(uid, None)

    await db.add_seen_ips(acc.provider, job.region, job.seen)

    try:
        await msg.edit_text(_progress_text(job, done=True))
    except Exception:
        pass

    if len(job.kept) > 1:
        lines = [f"🖥 <code>{s.ipv4[0]}</code>" for s in job.kept]
        await bot.send_message(uid, f"✅ <b>Итого куплено {len(job.kept)}:</b>\n\n" + "\n".join(lines)
                               + "\n\nПароли — в сообщениях выше и в «☁️ Мои серверы».")
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
