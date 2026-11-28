import asyncio
import ipaddress
import logging
import time
from dataclasses import asdict

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, Message
from aiogram.utils.text_decorations import html_decoration as hd

from .. import db, geo
from ..buyer import PurchaseJob, run_purchase
from ..scanner import ScanJob, run_scan
from ..config import config
from .. import notify, os_names, sshkey
from ..providers import PROVIDER_NAMES, RESERVE_PROVIDERS, SCOUT_PROVIDERS, SSH_KEY_PROVIDERS, SUBNET_PROVIDERS, Flavor, IpPool, ProviderError
from ..providers.base import normalize_ssh_key, prefix24, subnet_blocks
from ..providers.hostvds_public import money
from ..keyboards import PROVIDERS, back, grid, kb
from .accounts import accounts_view, make_client

router = Router()
log = logging.getLogger(__name__)

# user_id -> running job
JOBS: dict[int, "PurchaseJob | ScanJob"] = {}


def running_jobs() -> dict[int, "PurchaseJob | ScanJob"]:
    """Purchases / scans that are really running. Entries whose task is gone are dropped, so a failed
    start can never leave a "purchase in progress" behind."""
    for uid, job in list(JOBS.items()):
        if job.task is None or job.task.done():
            JOBS.pop(uid, None)
    return JOBS


def start_job(uid: int, job, coro) -> None:
    """Start a purchase / scan and register it (the task is kept, so it can't be garbage-collected)."""
    job.task = asyncio.create_task(coro)
    JOBS[uid] = job

MAX_IP_BUTTONS = 60


def is_admin(user_id: int) -> bool:
    """Admins pick subnets; everyone else buys from the subnets the admin saved (open bot = all admins)."""
    return not config.admin_ids or user_id in config.admin_ids


class Buy(StatesGroup):
    custom_prefix = State()
    count = State()
    ssh_key = State()
    scan_count = State()


async def _client_for(cb_or_msg, state: FSMContext):
    data = await state.get_data()
    acc = await db.get_account(cb_or_msg.from_user.id, data["account_id"])
    if not acc:
        raise ProviderError("Аккаунт не найден")
    return make_client(acc)


async def _error(cb: CallbackQuery, e: Exception):
    await cb.message.edit_text(f"❌ <b>Ошибка</b>\n\n<code>{hd.quote(str(e)[:500])}</code>",
                               reply_markup=kb(back("buy", "🔁 Начать заново"), back("menu", "🏠 Главное меню")))


# ---------- 1. provider ----------
@router.callback_query(F.data == "buy")
@router.callback_query(F.data.startswith("buy:mode:"))  # buttons from the earlier two-mode screen
async def choose_provider(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(is_admin=is_admin(cb.from_user.id))
    if cb.from_user.id in running_jobs():
        await cb.answer("⏳ Уже идёт покупка — дождитесь окончания или остановите её.", show_alert=True)
        return
    rows = [[Btn(text=f"☁️ {name}", callback_data=f"buy:prov:{key}")] for key, name in PROVIDERS.items()]
    await cb.message.edit_text("🛒 <b>Покупка сервера</b>\n\n<b>Шаг 1.</b> Выберите провайдера 👇",
                               reply_markup=kb(*rows, back("menu", "🏠 Главное меню")))
    await cb.answer()


# ---------- 2. account ----------
@router.callback_query(F.data.startswith("buy:prov:"))
async def choose_account(cb: CallbackQuery, state: FSMContext):
    provider = cb.data.split(":")[2]
    if provider not in PROVIDERS:  # a button of a provider that is no longer offered
        return await choose_provider(cb, state)
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
    await cb.message.edit_text("⏳ Проверяю, где сейчас можно купить сервер…")
    await cb.answer()
    data = await state.get_data()
    try:
        regions = await _load_regions(cb, state, data.get("provider", "hostvds"))
    except Exception as e:
        return await _error(cb, e)
    available = [(r, label) for r, label, ok, _ in regions if ok]
    unavailable = [(label, reason) for _, label, ok, reason in regions if not ok]
    if not available:
        text = "😔 <b>Сейчас нет доступных локаций</b>\n\n" + "\n".join(
            f"• {hd.quote(label)} — {hd.quote(reason)}" for label, reason in unavailable)
        await cb.message.edit_text(text[:4000], reply_markup=kb(
            [Btn(text="🔄 Проверить снова", callback_data=f"buy:refresh:{data['account_id']}")], back("buy")))
        return
    saved = {r for p, r, _l, _x in await db.list_presets() if p == data.get("provider", "hostvds")}
    available.sort(key=lambda x: x[0] not in saved)  # locations with working subnets first
    await state.update_data(regions=[r for r, _ in available], region_labels={r: label for r, label in available})
    text = (f"📍 <b>Шаг {_step(data, 'region')}.</b> Выберите локацию 👇\n\n"
            f"✅ Доступно: <b>{len(available)}</b> из {len(regions)}")
    if saved & {r for r, _ in available}:
        text += "\n⭐ — здесь есть рабочие подсети"
    if unavailable:
        text += "\n\n🚫 <i>Сейчас недоступны:</i>\n" + "\n".join(
            f"• {hd.quote(label)} — {hd.quote(reason[:60])}" for label, reason in unavailable)
    buttons = [Btn(text=f"{label} ⭐" if r in saved else label, callback_data=f"buy:reg:{i}")
               for i, (r, label) in enumerate(available)]
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
    if "regions" not in data:  # e.g. a button from an old message after the menu was opened
        await cb.answer("🔁 Начните покупку заново", show_alert=True)
        return await choose_provider(cb, state)
    region = data["regions"][int(cb.data.split(":")[2])]
    label = data["region_labels"][region]
    await state.update_data(region=region, region_label=label, is_admin=is_admin(cb.from_user.id))
    if not is_admin(cb.from_user.id):
        return await _user_ip_step(cb, state)
    await cb.message.edit_text(f"⏳ Смотрю свободные IP в <b>{hd.quote(label)}</b>…")
    await cb.answer()
    try:
        async with await _client_for(cb, state) as client:
            pools = await client.ip_pools(region)
    except Exception as e:
        log.warning("ip_pools failed: %s", e)
        pools = []
    if data["provider"] in SUBNET_PROVIDERS:
        pools = _mark_hits(pools, await db.seen_ips(data["provider"], region),
                           await db.subnets(data["provider"], region))
    else:
        pools = _merge_seen(pools, await db.seen_ips(data["provider"], region),
                            await db.subnets(data["provider"], region))
    full = [p for p in pools if p.free == 0]          # provider says: no free addresses there
    pools = [p for p in pools if p.free != 0]
    stats = await db.check_stats(data["provider"], region)
    dicts = [asdict(p) for p in pools]
    for d in dicts:  # earlier "only working" results inside this pool: passed / failed checks
        d["passed"], d["failed"] = _pool_checks(d["prefix"], stats)
    preset = await db.get_preset(data["provider"], region)  # the admin's saved choice comes pre-selected
    index = {d["prefix"]: i for i, d in enumerate(dicts)}
    await state.update_data(pools=dicts, selected=[index[p] for p in preset if p in index],
                            custom=[p for p in preset if p not in index], preset=preset,
                            full_blocks=len(full), ip_group=None)
    await render_ip_screen(cb.message, state, edit=True)


async def _user_ip_step(cb: CallbackQuery, state: FSMContext):
    """Users don't pick subnets: the purchase uses the ones the admin saved for this provider + location."""
    data = await state.get_data()
    preset = await db.get_preset(data["provider"], data["region"])
    if not preset:
        await cb.answer()
        return await cb.message.edit_text(
            f"😔 <b>Не удалось найти рабочую подсеть</b>\n\n📍 {hd.quote(data['region_label'])} · "
            f"{PROVIDER_NAMES[data['provider']]}\n\nДля этой локации пока нет рабочих подсетей.\n"
            "💬 Свяжитесь с админом — он добавит их, и покупка здесь заработает.\n"
            "📍 Или выберите другую локацию.",
            reply_markup=kb([Btn(text="💬 Написать админу", url=f"https://t.me/{config.admin_contact}")],
                            [Btn(text="📍 Другая локация", callback_data=f"buy:acc:{data['account_id']}")],
                            back("menu", "🏠 Главное меню")))
    await cb.message.edit_text("⏳ Готовлю покупку…")
    await cb.answer()
    targets, network_id = [], None
    if data["provider"] in ("hostvds", "ovh"):  # OpenStack: hunt IPs on the networks those subnets live in
        try:
            async with await _client_for(cb, state) as client:
                pools = [asdict(p) for p in await client.ip_pools(data["region"])]
            targets = _targets(pools, preset)
            network_id = next((t[0] for t in targets if t[0]), None)
        except Exception as e:
            log.info("ip_pools for the admin's subnets failed: %s", e)
    await state.update_data(prefixes=preset, targets=targets, network_id=network_id, pools=[])
    await ask_count(cb.message, state, edit=True)


def _admin_note(data: dict) -> str:
    preset = data.get("preset") or []
    if preset:
        return (f"\n\n📌 Для пользователей сохранено: <b>{len(preset)}</b> {_plural_subnets(len(preset))} "
                "(отмечены). Измените отметки и нажмите «📌 Сохранить»." )
    return ("\n\n📌 Для пользователей подсети <b>ещё не заданы</b> — без них они здесь не купят. "
            "Отметьте подсети и нажмите «📌 Сохранить для пользователей».")


def _admin_rows(data: dict, total: int) -> list[list[Btn]]:
    rows = []
    if total:
        rows.append([Btn(text=f"📌 Сохранить для пользователей · {total}", callback_data="buy:ipsave")])
    if data.get("preset"):
        rows.append([Btn(text="🗑 Убрать подсети пользователей", callback_data="buy:ipunsave")])
    return rows


@router.callback_query(F.data.in_({"buy:ipsave", "buy:ipunsave"}))
async def ip_save_preset(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔ Только для админа", show_alert=True)
    data = await state.get_data()
    if "pools" not in data:
        return await cb.answer("🔁 Начните покупку заново", show_alert=True)
    prefixes = ([] if cb.data == "buy:ipunsave" else
                [data["pools"][i]["prefix"] for i in data.get("selected", [])] + data.get("custom", []))
    old = set(await db.get_preset(data["provider"], data["region"]))
    label = data.get("region_label", data["region"])
    await db.set_preset(data["provider"], data["region"], label, prefixes)
    await state.update_data(preset=prefixes)
    new = [p for p in prefixes if p not in old]
    if new:  # new working subnets → everyone who ever wrote to the bot hears about it
        notify.announce_subnets(bot, cb.from_user.id, PROVIDER_NAMES[data["provider"]], label)
    await cb.answer((f"📌 Сохранено: {len(prefixes)} — пользователи покупают из этих подсетей"
                     + ("\n📣 Всем отправлено уведомление" if new else "")) if prefixes
                    else "🗑 Подсети убраны — пользователи здесь не купят", show_alert=True)
    await render_ip_screen(cb.message, state, edit=True)


@router.callback_query(F.data == "buy:ipany")
async def ip_any(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("⏳ Готовлю покупку…")
    await _skip_ip_step(cb, state, (await state.get_data())["region"])


async def _skip_ip_step(cb: CallbackQuery, state: FSMContext, region: str):
    """Any IP will do. For OpenStack (HostVDS) still pick a public network with free IPs."""
    await cb.answer()
    network_id = None
    try:
        async with await _client_for(cb, state) as client:
            pools = await client.ip_pools(region)
        nets = [p for p in pools if p.network_id and p.free != 0]
        if nets:
            network_id = max(nets, key=lambda p: p.free or 0).network_id
    except Exception as e:
        log.info("ip_pools in check mode failed: %s", e)
    await state.update_data(prefixes=[], targets=[], network_id=network_id, pools=[])
    await ask_count(cb.message, state, edit=True)


def _mark_hits(pools: list[IpPool], seen: list[str], subnets: list[str]) -> list[IpPool]:
    """Full subnet lists (geofeeds) cover a whole city: mark the subnets this very datacenter has
    actually handed out (from earlier purchases / scouting) and add ones missing from the list."""
    nets = [ipaddress.ip_network(p.prefix, strict=False) for p in pools]
    for cidr in subnets:
        net = ipaddress.ip_network(cidr, strict=False)
        if not any(net.subnet_of(n) for n in nets if n.version == net.version):
            pools.append(IpPool(str(net), None, [str(net)]))
            nets.append(net)
    for ip in seen:
        addr = ipaddress.ip_address(ip)
        for p, n in zip(pools, nets):
            if addr in n:
                p.hits += 1
                break
    return sorted(pools, key=lambda p: ipaddress.ip_network(p.prefix, strict=False))


def _pool_checks(prefix: str, stats: dict[str, list[int]]) -> tuple[int, int]:
    """Sum of earlier check results (/24 blocks) that fall inside this pool."""
    if not stats:
        return 0, 0
    if "/" in prefix:
        net = ipaddress.ip_network(prefix, strict=False)
        inside = [v for b, v in stats.items() if ipaddress.ip_address(b + ".0") in net]
    else:
        inside = [v for b, v in stats.items() if (b + ".").startswith(prefix + ".")]
    return sum(v[0] for v in inside), sum(v[1] for v in inside)


def _work_mark(p: dict) -> str:
    """🟢 checks passed here before · 🚫 only failed · "" unknown."""
    if p.get("passed"):
        return "🟢"
    if p.get("failed", 0) >= 2:
        return "🚫"
    return ""


def _work_note(p: dict) -> str:
    if p.get("passed"):
        return f" · 🟢 рабочих {p['passed']}"
    if p.get("failed", 0) >= 2:
        return " · 🚫 не проходит"
    return ""


def _subnet_mode(pools: list[dict]) -> bool:
    return bool(pools) and "/" in pools[0]["prefix"]


def _fit(lines: list[str], budget: int) -> str:
    """Join whole lines up to a size budget (cutting inside an HTML tag would break the message)."""
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            out.append(f"… и ещё {len(lines) - i} — все есть на кнопках ниже")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _attempts_text() -> str:
    return f"максимум {config.max_attempts} созданий" if config.max_attempts > 0 else "без лимита попыток"


def _wanted(prefixes: list[str], hidden: bool = False) -> str:
    if hidden:
        return "📌 <b>рабочие подсети</b> (подобраны админом)"
    return prefixes_text(prefixes) if prefixes else "🌐 <b>любой IP</b>"


def _step(data: dict, name: str) -> int:
    """Number of a step in the flow actually shown (network plan only when the tariff has one)."""
    order = ["provider", "region"] + (["ip"] if data.get("is_admin", True) else []) + ["count", "tariff"]
    order += (["netplan"] if data.get("has_netplan") else []) + ["os", "access"]
    return order.index(name) + 1


def _plural_subnets(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "подсеть"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "подсети"
    return "подсетей"


def _group(cidr: str) -> str:
    return ".".join(cidr.split(".")[:2])


def _blocks(cidr: str) -> int:
    return max(1, ipaddress.ip_network(cidr, strict=False).num_addresses // 256)


async def _send(msg: Message, text: str, markup, edit: bool):
    if edit:
        await msg.edit_text(text[:4000], reply_markup=markup)
    else:
        await msg.answer(text[:4000], reply_markup=markup)


async def render_subnet_screen(msg: Message, state: FSMContext, edit: bool):
    """Providers with a full subnet list (DigitalOcean): groups by the first two numbers → subnets with a mask."""
    data = await state.get_data()
    pools, selected, custom = data["pools"], set(data.get("selected", [])), data.get("custom", [])
    group = data.get("ip_group")
    total = len(selected) + len(custom)
    rows = []
    if group is None:
        groups: dict[str, list[int]] = {}
        for i, p in enumerate(pools):
            groups.setdefault(_group(p["prefix"]), []).append(i)
        hits = {g: sum(pools[i].get("hits", 0) for i in idx) for g, idx in groups.items()}
        works = {g: sum(pools[i].get("passed", 0) for i in idx) for g, idx in groups.items()}
        # 🟢 passed the check before, then subnets this datacenter has issued, then the rest
        groups = dict(sorted(groups.items(), key=lambda kv: (-works[kv[0]], -hits[kv[0]])))
        text = (f"🌐 <b>Шаг 3.</b> Выберите подсети\n"
                f"📍 {hd.quote(data['region_label'])} · подсетей: <b>{len(pools)}</b> · "
                f"блоков /24: <b>{sum(_blocks(p['prefix']) for p in pools)}</b>\n\n")
        lines = []
        for g, idx in groups.items():
            picked = sum(1 for i in idx if i in selected)
            lines.append(f"{'✅' if picked else '🟢' if works[g] else '▫️'} <code>{g}.#.#</code> — "
                         f"{len(idx)} {_plural_subnets(len(idx))}"
                         + (f", 🟢 рабочих {works[g]}" if works[g] else "")
                         + (f", выбрано {picked}" if picked else ""))
        text += _fit(lines, 3000)
        text += "\n\n💡 Откройте группу и отметьте подсети с нужной маской — бот купит сервер с IP из любой из них."
        if any(works.values()):
            text += "\n🟢 — здесь уже находились рабочие серверы: выбирайте их, так быстрее."
        buttons = [Btn(text=f"{'✅ ' if any(i in selected for i in idx) else '🟢 ' if works[g] else ''}"
                            f"{g}.#.# · {len(idx)}", callback_data=f"buy:ipg:{g}") for g, idx in list(groups.items())[:80]]
        rows = grid(buttons, 2)
        if any(works.values()):
            rows.append([Btn(text="🟢 Выбрать рабочие", callback_data="buy:ipwork")])
        rows.append([Btn(text="🌐 Любой IP", callback_data="buy:ipany")])
        if data.get("provider") in SCOUT_PROVIDERS:
            rows.append([Btn(text="🔍 Разведать доступные IP", callback_data="buy:scan")])
        rows.append([Btn(text="✏️ Своя подсеть", callback_data="buy:ip:custom"),
                     Btn(text="🔄 Сбросить", callback_data="buy:ipclr")])
    else:
        idx = [i for i, p in enumerate(pools) if _group(p["prefix"]) == group]
        text = (f"🌐 <b>Подсети {group}.#.#</b> · {len(idx)}\n📍 {hd.quote(data['region_label'])}\n\n"
                + _fit([f"{'✅' if i in selected else _work_mark(pools[i]) or '▫️'} "
                        f"<code>{pools[i]['prefix']}</code> · {_blocks(pools[i]['prefix'])} бл."
                        + _work_note(pools[i])
                        for i in idx], 3000))
        buttons = [Btn(text=f"{'✅ ' if i in selected else (_work_mark(pools[i]) + ' ').lstrip()}{pools[i]['prefix']}",
                       callback_data=f"buy:ipt:{i}") for i in idx[:80]]
        rows = grid(buttons, 2)
        if len(idx) > len(buttons):
            text += (f"\n\n💡 Кнопками показаны {len(buttons)} из {len(idx)} — отметьте всю группу кнопкой "
                     "«☑️ Все в группе» или впишите нужную подсеть через «✏️ Своя подсеть».")
        rows.append([Btn(text="☑️ Все в группе", callback_data="buy:ipgall"),
                     Btn(text="⬅️ К группам", callback_data="buy:ipg:-")])
    if custom:
        text += "\n\n✏️ Добавлено вручную: " + ", ".join(f"<code>{show_prefix(c)}</code>" for c in custom)
    text += f"\n\n☑️ Выбрано: <b>{total}</b>" + _admin_note(data)
    rows += _admin_rows(data, total)
    if total:
        rows.append([Btn(text=f"➡️ Дальше · выбрано {total}", callback_data="buy:ipok")])
    rows.append(back(f"buy:acc:{data['account_id']}"))
    await _send(msg, text, kb(*rows), edit)


@router.callback_query(F.data.startswith("buy:ipg:"))
async def ip_group(cb: CallbackQuery, state: FSMContext):
    group = cb.data.split(":", 2)[2]
    await state.update_data(ip_group=None if group == "-" else group)
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(F.data == "buy:ipwork")
async def ip_select_working(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected", [])) | {i for i, p in enumerate(data["pools"]) if p.get("passed")}
    await state.update_data(selected=sorted(selected))
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer("🟢 Отмечены подсети, где уже находились рабочие серверы")


@router.callback_query(F.data == "buy:ipgall")
async def ip_group_all(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = {i for i, p in enumerate(data["pools"]) if _group(p["prefix"]) == data.get("ip_group")}
    selected = set(data.get("selected", []))
    selected = selected - idx if idx <= selected else selected | idx  # second tap clears the group
    await state.update_data(selected=sorted(selected))
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


async def render_ip_screen(msg: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    if _subnet_mode(data["pools"]):
        return await render_subnet_screen(msg, state, edit)
    pools, selected, custom = data["pools"], set(data.get("selected", [])), data.get("custom", [])
    shown = pools[:MAX_IP_BUTTONS]
    text = (f"🌐 <b>Шаг 3.</b> Какие IP искать?\n"
            f"📍 {hd.quote(data['region_label'])} · блоков: <b>{len(pools)}</b>\n\n")
    if pools:
        text += "\n".join(f"{'✅' if i in selected else _work_mark(p) or '▫️'} <code>{p['prefix']}.#</code>"
                          + (f" · свободно {p['free']}" if p.get("free") else "")
                          + (f" · выпадал {p['hits']}×" if p.get("hits") else "") + _work_note(p)
                          for i, p in enumerate(pools[:150]))
        if len(pools) > 150:
            text += f"\n… и ещё {len(pools) - 150}"
        if len(pools) > len(shown):
            text += f"\n\nКнопками показаны первые {len(shown)} — остальные добавьте через «✏️ Свой IP»."
    else:
        text += ("🤷 Список подсетей пока неизвестен.\n"
                 "Бот запоминает IP, которые выдаёт провайдер, и после первой покупки покажет их здесь.\n"
                 "Пока добавьте IP вручную через «✏️ Свой IP»"
                 + (" или нажмите «🔍 Разведать доступные IP»." if data.get("provider") in SCOUT_PROVIDERS else "."))
    if data.get("full_blocks"):
        text += f"\n\n🙈 <i>Скрыто блоков без свободных IP: {data['full_blocks']}</i>"
    if custom:
        text += "\n\n✏️ Добавлено вручную: " + ", ".join(f"<code>{show_prefix(c)}</code>" for c in custom)
    total = len(selected) + len(custom)
    text += ("\n\n💡 Отметьте <b>один или несколько</b> блоков — бот купит сервер, как только выпадет любой из них."
             f"\n\n☑️ Выбрано: <b>{total}</b>") + _admin_note(data)
    buttons = [Btn(text=f"{'✅ ' if i in selected else (_work_mark(p) + ' ').lstrip()}{p['prefix']}.#",
                   callback_data=f"buy:ipt:{i}")
               for i, p in enumerate(shown)]
    rows = grid(buttons, 2)
    if len(shown) > 1:
        rows.append([Btn(text="☑️ Выбрать все", callback_data="buy:ipall"),
                     Btn(text="🔄 Сбросить", callback_data="buy:ipclr")])
    if any(p.get("passed") for p in pools):
        rows.append([Btn(text="🟢 Выбрать рабочие", callback_data="buy:ipwork")])
    rows.append([Btn(text="✏️ Свой IP", callback_data="buy:ip:custom"),
                 Btn(text="🌐 Любой IP", callback_data="buy:ipany")])
    if data.get("provider") in SCOUT_PROVIDERS:
        rows.append([Btn(text="🔍 Разведать доступные IP", callback_data="buy:scan")])
    rows += _admin_rows(data, total)
    if total:
        rows.append([Btn(text=f"➡️ Дальше · выбрано {total}", callback_data="buy:ipok")])
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


def _merge_seen(pools: list[IpPool], seen: list[str], subnets: list[str] = ()) -> list[IpPool]:
    """Adds /24 blocks of IPs the provider gave out before and of whole subnets learned from masks
    (for providers without a subnet API)."""
    known = {p.prefix: p for p in pools}
    for cidr in subnets:
        for prefix in subnet_blocks(cidr):
            pool = known.setdefault(prefix, IpPool(prefix, None, []))
            if pool.network_id is None and cidr not in pool.cidrs:
                pool.cidrs.append(cidr)
    for ip in seen:
        prefix = prefix24(ip)
        if prefix not in known:
            known[prefix] = IpPool(prefix, None, [])
        pool = known[prefix]
        pool.hits += 1
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
    await cb.message.edit_text("✏️ <b>Свой IP</b>\n\nОтправьте начало IP-адреса:\n"
                               "• <code>95.182.87</code> — блок 95.182.87.#\n"
                               "• <code>94.103</code> — любой 94.103.#.#\n"
                               "• <code>94</code> — любой 94.#.#.#\n"
                               "• <code>142.93.96.0/20</code> — подсеть с маской\n\n"
                               "💡 Можно несколько сразу: <code>95.182.87 94.103.80</code>",
                               reply_markup=kb(back("buy:ipback", "⬅️ Назад к списку")))
    await cb.answer()


@router.callback_query(F.data == "buy:ipback")
async def ip_back(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await render_ip_screen(cb.message, state, edit=True)
    await cb.answer()


def _parse_prefix(raw: str) -> str | None:
    if "/" in raw:  # a subnet with a mask: 142.93.96.0/20
        try:
            net = ipaddress.ip_network(raw.strip(), strict=False)
            return str(net) if net.version == 4 else None
        except ValueError:
            return None
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
        await message.answer("⚠️ Не понял формат. Пример: <code>95.182.87</code> или <code>94 95.182</code>")
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
        return await cb.answer("☝️ Отметьте хотя бы один IP", show_alert=True)
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
    text = (f"🔢 <b>Шаг {_step(data, 'count')}.</b> Сколько серверов нужно?\n\n"
            f"🎯 Ищем: {_wanted(data['prefixes'], not data.get('is_admin', True))}\n"
            + ("✅ Только рабочие — каждый сервер проверяется\n" if config.check_url else "")
            + "\n💡 Нажмите кнопку или отправьте число.")
    buttons = [Btn(text=str(n), callback_data=f"buy:cnt:{n}") for n in (1, 2, 3, 5, 10)]
    markup = kb(buttons, back("buy", "❌ Отмена"))
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Buy.count)
async def count_text(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 100:
        await message.answer("⚠️ Отправьте число от 1 до 100.")
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
        await msg.edit_text(f"❌ <b>Ошибка</b>\n\n<code>{hd.quote(str(e)[:500])}</code>",
                            reply_markup=kb(back("buy", "🔁 Начать заново")))
        return
    flavors = flavors[:30]
    await state.update_data(flavors=[asdict(f) for f in flavors])
    buttons = [Btn(text=f"{f.name} · {f.vcpus} vCPU · {f.ram_mb / 1024:g} GB"
                        + (f" · {f.disk_gb} GB" if f.disk_gb else "") + (f" · {f.price}" if f.price else ""),
                   callback_data=f"buy:fl:{i}")
               for i, f in enumerate(flavors)]
    await msg.edit_text(f"⚙️ <b>Шаг {_step(data, 'tariff')}.</b> Выберите тариф 👇\n\n💡 CPU · RAM · диск · цена в месяц",
                        reply_markup=kb(*grid(buttons, 1), back("buy", "❌ Отмена")))


# ---------- 6b. network plan (HostVDS) ----------
@router.callback_query(F.data.startswith("buy:fl:"))
async def flavor_selected(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    flavor = data["flavors"][int(cb.data.split(":")[2])]
    await state.update_data(flavor=flavor, network_plan=None, has_netplan=False)
    await cb.answer()
    if len(flavor.get("network_plans") or []) > 1:
        await state.update_data(has_netplan=True)
        await show_network_plans(cb.message, flavor, show_all=False, data=await state.get_data())
    else:
        await choose_image(cb, state)


def _plan_label(p: dict, currency: str, default: str) -> str:
    price = "бесплатно" if not p["price"] else f"{money(p['price'], currency)}/мес"
    return f"{'✔️ ' if p['name'] == default else ''}{p['title']} · {price}"


async def show_network_plans(msg: Message, flavor: dict, show_all: bool, data: dict | None = None):
    plans = flavor["network_plans"]
    shown = [(i, p) for i, p in enumerate(plans) if show_all or p["visible"]]
    rows = [[Btn(text=_plan_label(p, flavor.get("currency", "usd"), flavor.get("default_network_plan")),
                 callback_data=f"buy:np:{i}")] for i, p in shown]
    if not show_all and len(shown) < len(plans):
        rows.append([Btn(text=f"🔽 Показать все планы ({len(plans)})", callback_data="buy:npall")])
    text = (f"📶 <b>Шаг {_step({**(data or {}), 'has_netplan': True}, 'netplan')}.</b> Network plan для <b>{hd.quote(flavor['name'])}</b> 👇\n\n"
            "✔️ Limited — входит в тариф, трафик ограничен\n"
            "♾ Unlimited Shared — безлимитный трафик, общий канал\n"
            "🚀 Unlimited Dedicated — безлимит, выделенный канал (в «Все планы»)")
    await msg.edit_text(text, reply_markup=kb(*rows, back("buy", "❌ Отмена")))


@router.callback_query(F.data == "buy:npall")
async def network_plans_all(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_network_plans(cb.message, data["flavor"], show_all=True, data=data)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:np:"))
async def network_plan_selected(cb: CallbackQuery, state: FSMContext):
    flavor = (await state.get_data())["flavor"]
    await state.update_data(network_plan=flavor["network_plans"][int(cb.data.split(":")[2])])
    await cb.answer()
    await choose_image(cb, state)


# ---------- 7. image ----------
async def choose_image(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    flavor = data["flavor"]
    await cb.message.edit_text("⏳ Загружаю образы ОС…")
    try:
        async with await _client_for(cb, state) as client:
            images = await client.list_images(data["region"], Flavor(**flavor))
    except Exception as e:
        return await _error(cb, e)

    choices, other = os_names.simplify(images)
    await state.update_data(images=[{"id": c.id, "name": c.title, "distro": c.distro} for c in choices],
                            other_images=[asdict(i) for i in other[:40]])
    await _show_os(cb.message, choices, bool(other), await state.get_data())


async def _show_os(msg: Message, choices, has_other: bool, data: dict | None = None):
    buttons = [Btn(text=c.title, callback_data=f"buy:img:{n}") for n, c in enumerate(choices[:40])]
    rows = grid(buttons, 2)
    if has_other:
        rows.append([Btn(text="📦 Другие образы", callback_data="buy:imgother")])
    n = _step(data or {}, "os")
    text = f"💿 <b>Шаг {n}.</b> Выберите операционную систему 👇" if choices else f"💿 <b>Шаг {n}.</b> Выберите образ 👇"
    await msg.edit_text(text, reply_markup=kb(*rows, back("buy", "❌ Отмена")))


@router.callback_query(F.data == "buy:imgother")
async def other_images(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = [Btn(text=i["name"][:45], callback_data=f"buy:imgo:{n}") for n, i in enumerate(data["other_images"])]
    await cb.message.edit_text("📦 <b>Другие образы</b>\n\nПриложения, панели и старые версии 👇",
                               reply_markup=kb(*grid(buttons, 1), [Btn(text="⬅️ К списку ОС", callback_data="buy:imgback")]))
    await cb.answer()


@router.callback_query(F.data == "buy:imgback")
async def images_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    choices = [os_names.OsChoice(i["id"], i["name"], i["name"], i.get("distro", "")) for i in data["images"]]
    await _show_os(cb.message, choices, bool(data.get("other_images")), data)
    await cb.answer()


# ---------- 8. confirm ----------
@router.callback_query(F.data.startswith("buy:img:") | F.data.startswith("buy:imgo:"))
async def image_selected(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kind, idx = cb.data.split(":")[1:3]
    image = (data["other_images"] if kind == "imgo" else data["images"])[int(idx)]
    await state.update_data(image=image, ssh_key_id=None)
    await cb.answer()
    if data["provider"] in SSH_KEY_PROVIDERS:
        await show_access(cb.message, state, cb.from_user.id, edit=True)
    else:
        await render_confirm(cb.message, state, edit=True)


# ---------- 8. access: password or SSH key ----------
async def show_access(msg: Message, state: FSMContext, user_id: int, edit: bool):
    keys = await db.list_ssh_keys(user_id)
    rows = [[Btn(text="🔑 Пароль root", callback_data="buy:auth:pw")]]
    for key_id, name, _ in keys:
        rows.append([Btn(text=f"🗝 SSH-ключ «{name[:30]}»", callback_data=f"buy:auth:k:{key_id}"),
                     Btn(text="🗑", callback_data=f"buy:auth:del:{key_id}")])
    rows.append([Btn(text="➕ Добавить SSH-ключ", callback_data="buy:auth:add")])
    rows.append(back("buy", "❌ Отмена"))
    text = (f"🔐 <b>Шаг {_step(await state.get_data(), 'access')}.</b> Как входить на сервер?\n\n"
            "🔑 <b>Пароль root</b> — бот пришлёт пароль вместе с IP.\n"
            "🗝 <b>SSH-ключ</b> — на сервер добавится ваш публичный ключ, вход без пароля: "
            "<code>ssh root@IP</code>.")
    if keys:
        text += f"\n\n💾 Сохранено ключей: <b>{len(keys)}</b>"
    await _send(msg, text, kb(*rows), edit)


@router.callback_query(F.data == "buy:auth:pw")
async def auth_password(cb: CallbackQuery, state: FSMContext):
    await state.update_data(ssh_key_id=None)
    await cb.answer()
    await render_confirm(cb.message, state, edit=True)


@router.callback_query(F.data.startswith("buy:auth:k:"))
async def auth_key(cb: CallbackQuery, state: FSMContext):
    key = await db.get_ssh_key(cb.from_user.id, int(cb.data.split(":")[3]))
    if not key:
        return await cb.answer("Ключ не найден", show_alert=True)
    await state.update_data(ssh_key_id=key[0])
    await cb.answer()
    await render_confirm(cb.message, state, edit=True)


@router.callback_query(F.data.startswith("buy:auth:del:"))
async def auth_delete_key(cb: CallbackQuery, state: FSMContext):
    await db.delete_ssh_key(cb.from_user.id, int(cb.data.split(":")[3]))
    await cb.answer("🗑 Ключ удалён")
    await show_access(cb.message, state, cb.from_user.id, edit=True)


@router.callback_query(F.data == "buy:auth:add")
async def auth_add_key(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Buy.ssh_key)
    await cb.message.edit_text(
        "🗝 <b>Добавить SSH-ключ</b>\n\n"
        "Отправьте <b>публичный</b> ключ одной строкой — он начинается с <code>ssh-ed25519</code>, "
        "<code>ssh-rsa</code> или <code>ecdsa-sha2-…</code>.\n\n"
        "💻 Показать свой ключ: <code>cat ~/.ssh/id_ed25519.pub</code>\n"
        "🆕 Создать новый: <code>ssh-keygen -t ed25519</code>\n\n"
        "⛔ Никогда не отправляйте <b>приватный</b> ключ (файл без <code>.pub</code>).",
        reply_markup=kb(back("buy:auth:back", "⬅️ Назад")))
    await cb.answer()


@router.callback_query(F.data == "buy:auth:back")
async def auth_back(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await cb.answer()
    await show_access(cb.message, state, cb.from_user.id, edit=True)


@router.message(Buy.ssh_key)
async def auth_key_text(message: Message, state: FSMContext):
    text = message.text or ""
    if "PRIVATE KEY" in text:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("⛔ <b>Это приватный ключ</b> — сообщение удалено. Отправьте файл с <code>.pub</code> "
                             "на конце (публичный ключ). Приватный ключ лучше заменить, раз он был отправлен.")
        return
    key = normalize_ssh_key(text)
    if not key:
        await message.answer("⚠️ Это не похоже на публичный SSH-ключ. Он выглядит так:\n"
                             "<code>ssh-ed25519 AAAAC3Nza… user@pc</code>")
        return
    parts = key.split(" ")
    name = parts[2] if len(parts) > 2 else f"{parts[0].replace('ssh-', '')} …{parts[1][-6:]}"
    key_id = await db.add_ssh_key(message.from_user.id, name[:40], key)
    await state.set_state(None)
    await state.update_data(ssh_key_id=key_id)
    await message.answer(f"✅ Ключ «{hd.quote(name[:40])}» сохранён — он будет доступен и в следующих покупках.")
    await render_confirm(message, state, edit=False)


async def render_confirm(msg: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    image = data["image"]
    f = data["flavor"]
    np = data.get("network_plan")
    lines = [
        "🧾 <b>Проверьте заказ</b>\n",
        f"☁️ Провайдер: <b>{PROVIDER_NAMES[data['provider']]}</b>",
        f"📍 Локация: <b>{hd.quote(data['region_label'])}</b>",
        f"🎯 Ищем IP: {_wanted(data['prefixes'], not data.get('is_admin', True))}",
        f"⚙️ Тариф: <b>{hd.quote(f['name'])}</b> · {f['vcpus']} vCPU · {f['ram_mb'] / 1024:g} GB"
        + (f" · {f['price']}" if f.get("price") else ""),
    ]
    if np:
        price = f" · {money(np['price'], f.get('currency', 'usd'))}/мес" if np["price"] else " · бесплатно"
        lines.append(f"📶 Network plan: <b>{hd.quote(np['title'])}</b>{price}")
    key = await db.get_ssh_key(msg.chat.id, data["ssh_key_id"]) if data.get("ssh_key_id") else None
    lines += [f"💿 ОС: <b>{hd.quote(image['name'])}</b>",
              f"🔐 Доступ: <b>{'SSH-ключ «' + hd.quote(key[1]) + '»' if key else 'пароль root'}</b>",
              f"🔢 Количество: <b>{data['count']}</b>", ""]
    if data["provider"] not in SSH_KEY_PROVIDERS:
        lines.insert(-1, "ℹ️ У этого провайдера ключ при заказе передать нельзя — вход по паролю root.")
    pools = data.get("pools") or []
    if data.get("network_id") and data["provider"] == "hostvds":
        lines.append("⚡ <b>Быстрый режим:</b> IP подбирается бесплатно через сетевые порты, "
                     "сервер покупается только на подходящий IP.")
    elif data["provider"] in RESERVE_PROVIDERS and data["prefixes"]:
        lines.append("⚡ <b>Сначала только IP:</b> бот берёт резервные IP без серверов и покупает сервер "
                     "только на IP из выбранных подсетей — лимит серверов почти не тратится.")
    lines.append("✅ Только рабочие: каждый подходящий сервер проверяется, не прошедшие проверку удаляются.\n"
                 if config.check_url else "")
    lines.append((f"💸 Серверы с чужим IP удаляются сразу ({_attempts_text()}). " if data["prefixes"]
                  else f"💸 Подбор {_attempts_text()}. ")
                 + "Каждое создание может стоить денег — остановить можно кнопкой ⛔.")
    par = data.get("parallel") or config.parallel
    lines.append(f"\n⚡ <b>Скорость:</b> {par} {_plural_servers(par)} одновременно — больше серверов сразу, "
                 "быстрее найдётся рабочий (и тем больше пробных серверов за раз).")
    text = "\n".join(lines)
    speed = [Btn(text=f"{'✅ ' if n == par else ''}⚡{n}", callback_data=f"buy:par:{n}") for n in SPEEDS]
    await _send(msg, text, kb(speed, [Btn(text="🚀 Купить", callback_data="buy:go")], back("buy", "❌ Отмена")), edit)


SPEEDS = (1, 3, 5, 10)


def _plural_servers(n: int) -> str:
    return "сервер" if n % 10 == 1 and n % 100 != 11 else "сервера" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "серверов"


@router.callback_query(F.data.startswith("buy:par:"))
async def set_speed(cb: CallbackQuery, state: FSMContext):
    n = int(cb.data.split(":")[2])
    await state.update_data(parallel=n)
    await cb.answer(f"⚡ {n} одновременно")
    await render_confirm(cb.message, state, edit=True)


# ---------- 9. run ----------
def _progress_text(job: PurchaseJob, done: bool = False, label: str = "") -> str:
    head = "🏁 <b>Покупка завершена</b>" if done else "⏳ <b>Идёт покупка…</b>"
    text = f"{head}\n\n📍 {hd.quote(label or job.region)}\n"
    text += f"🎯 Ищем: {_wanted(job.prefixes, job.hide_prefixes) if job.hide_prefixes else prefixes_text(job.prefixes, 10) if job.prefixes else '🌐 любой IP'}\n"
    if job.check_url:
        text += f"🔎 Проверено: <b>{job.checked}</b>"
        text += f"   ⏭ Пропущено (нерабочие блоки): <b>{job.skipped_bad}</b>\n" if job.skipped_bad else "\n"
    if job.ports_checked:
        text += f"⚡ IP проверено без сервера: <b>{job.ports_checked}</b>   🎯 В подсетях: <b>{job.port_matches}</b>\n"
    text += (f"✅ Готово: <b>{len(job.kept)}/{job.count}</b>   "
             f"🆕 Создано: <b>{job.attempts}</b>   🗑 Удалено: <b>{job.deleted}</b>"
             + (f"   ⏸ Держу: <b>{len(job.held)}</b>" if job.held else "") + "\n")
    if job.log:
        text += "\n" + "\n".join(job.log[-12:])
    return text[:4000]


async def send_safe(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Telegram send with retries (flood limits, network hiccups) — a lost message here means a lost password."""
    for attempt in range(5):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            log.exception("send_message failed (attempt %s)", attempt + 1)
            await asyncio.sleep(2 * (attempt + 1))
    return False


def server_card(ip: str, password: str | None, name: str = "", label: str = "", key_name: str = "") -> str:
    lines = [f"🖥 <code>{ip}</code>"]
    if key_name:
        lines.append(f"🗝 Вход по SSH-ключу «{hd.quote(key_name)}»")
    if password:
        lines.append(f"🔑 <code>{hd.quote(password)}</code>")
    lines.append(f"💻 <code>ssh root@{ip}</code>")
    if name or label:
        lines.append(f"🏷 {hd.quote(name)}{' · ' + hd.quote(label) if label else ''}")
    return "\n".join(lines)


@router.callback_query(F.data == "buy:go")
async def go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    if uid in running_jobs():
        await cb.answer("⏳ Покупка уже идёт", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account(uid, data["account_id"])
    job = PurchaseJob(
        region=data["region"], flavor=Flavor(**data["flavor"]), image_id=data["image"]["id"],
        network_id=data.get("network_id"), prefixes=data["prefixes"], count=data["count"],
        targets=[tuple(t) for t in data.get("targets", [])], max_port_attempts=config.max_port_attempts,
        metadata={"network_plan": (data.get("network_plan") or {}).get("name"),
                  "os_distro": data["image"].get("distro"), "kind": data["flavor"].get("kind")}
        if data["provider"] == "hostvds" else {},
        port_parallel=config.port_parallel,
        max_attempts=config.max_attempts, parallel=data.get("parallel") or config.parallel,
        active_timeout=config.active_timeout, boot_volume_gb=config.boot_volume_gb,
        hide_prefixes=not data.get("is_admin", True),
    )
    key = await db.get_ssh_key(uid, data["ssh_key_id"]) if data.get("ssh_key_id") else None
    if key and data["provider"] in SSH_KEY_PROVIDERS:
        job.ssh_key, job.ssh_key_name = key[2], key[1]
    if config.check_url:  # every purchase: matching servers are checked before they are sent
        job.check_url, job.check_timeout = config.check_url, config.check_ssh_timeout
        job.block_stats = await db.check_stats(data["provider"], data["region"])
        if data["provider"] in SSH_KEY_PROVIDERS:
            job.bot_key = sshkey.bot_key()  # the bot's own key rides along so it can log in and check
    cancel_kb = kb([Btn(text="⛔ Остановить", callback_data="buy:cancel")])
    start_job(uid, job, _run(bot, cb.message, uid, acc, job, cancel_kb, data.get("region_label", "")))
    await cb.answer()
    try:
        await cb.message.edit_text(_progress_text(job, label=data.get("region_label", "")), reply_markup=cancel_kb)
    except Exception:
        pass  # the purchase runs anyway; progress updates will retry


async def _run(bot: Bot, msg: Message, uid: int, acc: db.Account, job: PurchaseJob, cancel_kb, label: str = ""):
    last_edit = 0.0
    saved: set[str] = set()
    found_lock = asyncio.Lock()

    async def progress(j: PurchaseJob):
        nonlocal last_edit
        if time.time() - last_edit < 3:
            return
        last_edit = time.time()
        try:
            await msg.edit_text(_progress_text(j, label=label), reply_markup=cancel_kb)
        except Exception:
            pass

    async def save(server) -> None:
        if server.id not in saved:
            await db.add_server(uid, acc.id, job.region, server.id, server.name, server.ipv4[0], server.admin_pass)
            saved.add(server.id)

    async def found(server):
        # Save and send each server right away — one at a time, so nothing gets lost.
        async with found_lock:
            try:
                await save(server)
            except Exception:
                log.exception("saving server %s failed", server.id)
            head = "✅ <b>Рабочий сервер!</b>" if job.check_url else "🎉 <b>Нашёлся IP!</b>"
            await send_safe(bot, uid, f"{head}  ({len(job.kept)}/{job.count})\n\n"
                            + server_card(server.ipv4[0], server.admin_pass, server.name, label, job.ssh_key_name)
                            + ("\n✅ Проверка пройдена" if job.check_url else "")
                            + ("\n\n⏱ Ключ начнёт работать примерно через минуту после запуска." if job.ssh_key
                               else "\n\n⏱ Пароль начнёт работать примерно через минуту после запуска."
                               if acc.provider in ("hostvds", "digitalocean", "ovh") else ""))

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
    await db.add_subnets(acc.provider, job.region, job.subnets)
    await db.add_check_results(acc.provider, job.region, job.check_results)
    for server in job.kept:  # make sure every bought server is in "My servers"
        try:
            await save(server)
        except Exception:
            log.exception("saving server %s failed", server.id)

    try:
        await msg.edit_text(_progress_text(job, done=True, label=label))
    except Exception:
        pass

    # Final summary always repeats IP + password of every server.
    if job.kept:
        cards = "\n\n".join(server_card(srv.ipv4[0], srv.admin_pass, key_name=job.ssh_key_name) for srv in job.kept)
        await send_safe(bot, uid, f"📦 <b>Итого куплено: {len(job.kept)}</b>\n\n{cards}\n\n"
                        "💡 Нажмите на IP или пароль, чтобы скопировать. Всё сохранено в «☁️ Мои серверы».")
    reason = ("⛔ остановлено вами" if job.cancelled else
              f"❗ ошибка: {hd.quote(job.error)}" if job.error else
              "🔁 достигнут лимит попыток" if len(job.kept) < job.count else None)
    if reason:
        await send_safe(bot, uid, f"⚠️ <b>Куплено {len(job.kept)} из {job.count}</b>\n{reason}")
    await send_safe(bot, uid, "👇 Что дальше?", reply_markup=kb(
        [Btn(text="☁️ Мои серверы", callback_data="servers"), Btn(text="🛒 Купить ещё", callback_data="buy")],
        back("menu", "🏠 Главное меню")))


@router.callback_query(F.data == "buy:cancel")
async def cancel(cb: CallbackQuery):
    job = running_jobs().get(cb.from_user.id)
    if job:
        job.cancelled = True
        await cb.answer("⛔ Останавливаю после текущей попытки…", show_alert=True)
    else:
        await cb.answer("🤷 Нет активной покупки")


# ---------- IP scouting (Selectel VDS: no subnet API) ----------
SCAN_SIZES = (5, 10, 20)
SCAN_CURRENCY = {"selectel": "₽", "timeweb": "₽", "aeza": "€", "serverspace": "₽", "ovh": "€", "gcp": "$",
                 "digitalocean": "$", "linode": "$", "civo": "$", "hetzner": "€", "vultr": "$", "upcloud": "€", "gcore": "€"}


@router.callback_query(F.data == "buy:scan")
async def scan_offer(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await cb.message.edit_text("⏳ Считаю стоимость разведки…")
    await cb.answer()
    if data["provider"] in RESERVE_PROVIDERS:
        return await _scan_offer_ips(cb, state, data)
    try:
        async with await _client_for(cb, state) as client:
            flavor, hour = await client.cheapest_plan(data["region"])
            images = await client.list_images(data["region"], flavor)
    except Exception as e:
        return await _error(cb, e)
    if not images:
        return await _error(cb, Exception("Нет образов для пробных серверов"))
    choices, _ = os_names.simplify(images)
    image_id = choices[0].id if choices else images[0].id
    await state.update_data(scan={"flavor": asdict(flavor), "image_id": image_id, "hour": hour})
    cur = SCAN_CURRENCY.get(data["provider"], "₽")
    par, speed = _scan_speed(data)
    rows = [[Btn(text=f"🔍 {n} серверов · ≈{n * hour:.2f} {cur}", callback_data=f"buy:scango:{n}")]
            for n in SCAN_SIZES]
    rows.append([Btn(text=f"✏️ Своё число (до {config.scan_max})", callback_data="buy:scannum")])
    rows.append(speed)
    rows.append(back("buy:ipback", "⬅️ Назад к выбору IP"))
    await cb.message.edit_text(
        "🔍 <b>Разведка IP</b>\n"
        f"📍 {hd.quote(data['region_label'])}\n\n"
        f"У {PROVIDER_NAMES[data['provider']]} нет списка подсетей, поэтому бот один раз проверит их сам:\n"
        "1️⃣ создаст самый дешёвый сервер (по скорости — несколько сразу)\n"
        "2️⃣ запишет, какой IP он получил\n"
        "3️⃣ сразу удалит сервер и перейдёт к следующему\n"
        "4️⃣ в конце пришлёт отчёт: какие блоки <code>a.b.c.#</code> сейчас выдаются\n\n"
        f"⚙️ Тариф для проверки: <b>{hd.quote(flavor.name)}</b> · {hour:g} {cur}/час\n"
        "💸 Оплата почасовая — каждый пробный сервер обходится примерно в час тарифа.\n\n"
        f"⚡ Скорость: <b>{par}</b> одновременно — в {par} раз быстрее, цена та же (платите за каждый сервер, "
        "а не за скорость). Упрётся в лимит аккаунта — бот подождёт.\n\n"
        "💡 Чем больше серверов, тем полнее картина. Найденные блоки появятся в выборе IP."
        + ("\n\n⚡ Сначала бот попробует брать IP <b>без серверов</b> (почти бесплатно) — серверы "
           "понадобятся, только если провайдер это не разрешит." if data["provider"] == "ovh" else ""),
        reply_markup=kb(*rows))


def _scan_speed(data: dict) -> tuple[int, list[Btn]]:
    par = data.get("scan_par") or config.scan_parallel
    return par, [Btn(text=f"{'✅ ' if n == par else ''}⚡{n}", callback_data=f"buy:scanpar:{n}") for n in SPEEDS]


@router.callback_query(F.data.startswith("buy:scanpar:"))
async def scan_speed(cb: CallbackQuery, state: FSMContext):
    await state.update_data(scan_par=int(cb.data.split(":")[2]))
    await scan_offer(cb, state)


async def _scan_offer_ips(cb: CallbackQuery, state: FSMContext, data: dict):
    """Hetzner / Vultr: scouting reserves standalone IPs only — no servers, almost free."""
    stub = Flavor("-", "-", 0, 0, 0)
    await state.update_data(scan={"flavor": asdict(stub), "image_id": "-", "hour": 0})
    par, speed = _scan_speed(data)
    rows = [[Btn(text=f"🔍 {n} IP", callback_data=f"buy:scango:{n}")] for n in (20, 50, 100)]
    rows.append([Btn(text=f"✏️ Своё число (до {config.scan_max})", callback_data="buy:scannum")])
    rows.append(speed)
    rows.append(back("buy:ipback", "⬅️ Назад к выбору IP"))
    await cb.message.edit_text(
        "🔍 <b>Разведка IP</b>\n"
        f"📍 {hd.quote(data['region_label'])}\n\n"
        f"У {PROVIDER_NAMES[data['provider']]} можно взять IP без сервера, поэтому разведка почти бесплатная:\n"
        "1️⃣ бот резервирует IP\n"
        "2️⃣ записывает адрес\n"
        "3️⃣ сразу освобождает его и берёт следующий\n"
        "4️⃣ в конце присылает отчёт: какие блоки сейчас выдаются\n\n"
        f"⚡ Скорость: <b>{par}</b> IP одновременно.\n"
        "🔎 Найденные подсети появятся в выборе IP.",
        reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("buy:scango:"))
async def scan_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    await start_scan(cb.message, state, bot, cb.from_user.id, int(cb.data.split(":")[2]), edit=True)


@router.callback_query(F.data == "buy:scannum")
async def scan_custom(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "scan" not in data:
        return await cb.answer("🔁 Откройте разведку заново", show_alert=True)
    await state.set_state(Buy.scan_count)
    hour = data["scan"]["hour"]
    cur = SCAN_CURRENCY.get(data["provider"], "₽")
    await cb.message.edit_text(
        f"✏️ <b>Сколько {'IP' if data['provider'] in RESERVE_PROVIDERS else 'серверов'} проверить?</b>\n\n"
        f"Отправьте число от 1 до {config.scan_max}, например <code>100</code>.\n\n"
        + (f"💸 Примерно {hour:g} {cur} за каждый сервер · 100 серверов ≈ {100 * hour:.2f} {cur}\n"
           if data["provider"] not in RESERVE_PROVIDERS else "💸 Проверяются только IP, без серверов — почти бесплатно.\n")
        + ("" if data["provider"] in RESERVE_PROVIDERS
           else f"⏱ Одновременно: {data.get('scan_par') or config.scan_parallel} (скорость выбирается на прошлом экране)."),
        reply_markup=kb(back("buy:scan", "⬅️ Назад")))
    await cb.answer()


@router.message(Buy.scan_count)
async def scan_custom_set(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= config.scan_max:
        await message.answer(f"⚠️ Отправьте число от 1 до {config.scan_max}")
        return
    await state.set_state(None)
    await start_scan(message, state, bot, message.from_user.id, int(raw), edit=False)


async def start_scan(msg: Message, state: FSMContext, bot: Bot, uid: int, samples: int, edit: bool):
    if uid in running_jobs():
        await msg.answer("⏳ Уже идёт покупка или разведка — дождитесь окончания или остановите её.")
        return
    data = await state.get_data()
    if "scan" not in data:
        await msg.answer("🔁 Откройте разведку заново")
        return
    acc = await db.get_account(uid, data["account_id"])
    job = ScanJob(region=data["region"], flavor=Flavor(**data["scan"]["flavor"]), image_id=data["scan"]["image_id"],
                  samples=samples, parallel=data.get("scan_par") or config.scan_parallel)
    cancel_kb = kb([Btn(text="⛔ Остановить", callback_data="buy:cancel")])
    label = data["region_label"]
    if edit:
        try:
            await msg.edit_text(_scan_text(job, label), reply_markup=cancel_kb)
        except Exception:
            pass
        progress_msg = msg
    else:
        progress_msg = await msg.answer(_scan_text(job, label), reply_markup=cancel_kb)
    region_idx = data["regions"].index(data["region"])
    start_job(uid, job, _run_scan(bot, progress_msg, uid, acc, job, cancel_kb, label, region_idx))


def _fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes >= 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes} мин" if minutes else "меньше минуты"


def _scan_text(job: ScanJob, label: str, done: bool = False) -> str:
    head = "🏁 <b>Разведка завершена</b>" if done else "🔍 <b>Идёт разведка IP…</b>"
    text = (f"{head}\n\n📍 {hd.quote(label)}\n"
            f"🆕 Создано: <b>{job.created}/{job.samples}</b>   🗑 Удалено: <b>{job.deleted}</b>   "
            f"🌐 IP: <b>{len(job.ips)}</b>\n")
    blocks = job.blocks()
    if blocks:
        text += f"📊 Блоков: <b>{len(blocks)}</b> · чаще всего <code>{blocks[0][0]}.#</code> ({blocks[0][1]})\n"
    eta = job.eta_seconds()
    if not done and eta:
        text += f"⏱ Осталось примерно: <b>{_fmt_duration(eta)}</b>\n"
    if job.log:
        text += "\n" + "\n".join(job.log[-12:])
    return text[:4000]


async def _run_scan(bot: Bot, msg: Message, uid: int, acc: db.Account, job: ScanJob, cancel_kb, label: str,
                    region_idx: int):
    last = 0.0

    async def progress(j: ScanJob):
        nonlocal last
        if time.time() - last < 3:
            return
        last = time.time()
        try:
            await msg.edit_text(_scan_text(j, label), reply_markup=cancel_kb)
        except Exception:
            pass

    try:
        async with make_client(acc) as client:
            await run_scan(client, job, progress)
    except Exception as e:
        log.exception("scan failed")
        job.error = str(e)
    finally:
        JOBS.pop(uid, None)

    if job.ips:
        await db.add_seen_ips(acc.provider, job.region, job.ips)
    await db.add_subnets(acc.provider, job.region, job.subnets)
    try:
        await msg.edit_text(_scan_text(job, label, done=True))
    except Exception:
        pass

    blocks = job.blocks()
    lines = [f"📊 <b>Отчёт разведки</b>\n📍 {hd.quote(label)}\n",
             (f"🌐 Проверено IP: <b>{job.created}</b>" if acc.provider in RESERVE_PROVIDERS else
              f"🖥 Проверено серверов: <b>{job.created}</b> · получено IP: <b>{len(job.ips)}</b>")]
    if blocks:
        lines.append(f"🌐 Найдено блоков: <b>{len(blocks)}</b>\n")
        lines += [f"▫️ <code>{p}.#</code> — {n} из {len(job.ips)} ({n * 100 // len(job.ips)}%)" for p, n in blocks]
        if job.subnets:
            lines.append("\n🧩 <b>Подсети</b> (по маске IP — все их блоки добавлены в выбор IP):")
            lines += [f"▫️ <code>{c}</code>" for c in job.subnets[:20]]
        lines.append("\n💡 Чем чаще блок выпадал, тем быстрее бот купит сервер с IP из него.\n"
                     "✅ Блоки сохранены — они уже есть в выборе IP.")
    else:
        lines.append("\n🤷 Ни одного IP получить не удалось.")
    if job.cancelled:
        lines.append("\n⛔ Разведка остановлена вами.")
    if job.error:
        lines.append(f"\n❗ Ошибка: <code>{hd.quote(job.error[:300])}</code>")
    if job.leftovers:
        lines.append("\n⚠️ Не удалось удалить серверы: " + ", ".join(f"<code>{x}</code>" for x in job.leftovers)
                     + f" — удалите их в панели {PROVIDER_NAMES.get(acc.provider, 'провайдера')}, "
                       "чтобы они не списывали деньги.")
    await send_safe(bot, uid, "\n".join(lines)[:4000], reply_markup=kb(
        [Btn(text="➡️ Выбрать IP", callback_data=f"buy:reg:{region_idx}")],
        back("menu", "🏠 Главное меню")))


# ---------- admin: subnets saved for users ----------
@router.callback_query(F.data == "presets")
@router.callback_query(F.data.startswith("pst:del:"))
async def presets(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔ Только для админа", show_alert=True)
    items = await db.list_presets()
    note = ""
    if cb.data.startswith("pst:del:"):
        i = int(cb.data.split(":")[2])
        if i < len(items):
            prov, region, label, _ = items[i]
            await db.set_preset(prov, region, label, [])
            note = f"🗑 Убрано: {PROVIDER_NAMES.get(prov, prov)} · {hd.quote(label)}\n\n"
            items = await db.list_presets()
    text = "📌 <b>Подсети для пользователей</b>\n\n" + note
    if not items:
        text += "📭 Пока ничего не сохранено — пользователи нигде не могут купить.\n"
    for prov, _region, label, prefixes in items:
        text += (f"☁️ <b>{PROVIDER_NAMES.get(prov, prov)}</b> · {hd.quote(label)}\n"
                 f"   {prefixes_text(prefixes, 8)}\n")
    text += ("\n✏️ Добавить или изменить: «🛒 Купить сервер» → провайдер → локация → отметьте подсети → "
             "«📌 Сохранить для пользователей».")
    rows = [[Btn(text=f"🗑 {PROVIDER_NAMES.get(p, p)} · {label}"[:60], callback_data=f"pst:del:{i}")]
            for i, (p, _r, label, _x) in enumerate(items)]
    await cb.message.edit_text(text[:4000], reply_markup=kb(*rows, back("menu", "🏠 Главное меню")))
    await cb.answer()
