"""HostVDS public site API (no login needed) — what the hostvds.com control panel itself uses:

GET https://hostvds.com/api/regions/            regions: city, country, availability, free IPv4 per network
GET https://hostvds.com/api/plans/?region=X     tariffs: flavor_uuid, prices, stock, network plans (network_opts)

A server is created through the normal OpenStack API with flavorRef=plan.flavor_uuid and
metadata {"network_plan": <name>, "os_distro": ..., "kind": plan.kind} — exactly like the panel does.
"""
import time

import aiohttp

from ..config import config

_CACHE: dict[str, tuple[float, object]] = {}
TTL = 60

CITY_RU = {
    "dallas": "Даллас", "silicon-valley": "Силиконовая долина", "amsterdam": "Амстердам", "paris": "Париж",
    "riga": "Рига", "hong-kong": "Гонконг", "kansas": "Канзас-Сити", "hel": "Хельсинки", "helsinki": "Хельсинки",
    "frankfurt": "Франкфурт", "london": "Лондон", "warsaw": "Варшава", "moscow": "Москва", "singapore": "Сингапур",
    "tokyo": "Токио", "new-york": "Нью-Йорк", "miami": "Майами", "los-angeles": "Лос-Анджелес",
}

# Display names exactly as in the HostVDS panel
PLAN_NAMES = {
    "unlim-shared-100": "Unlimited Shared 100 Mbps", "unlim-shared-1000": "Unlimited Shared 1 Gbps",
    "unlim-shared-10000": "Unlimited Shared 10 Gbps", "unlim-dedicated-1000": "Unlimited Dedicated 1 Gbps",
    "unlim-dedicated-10000": "Unlimited Dedicated 10 Gbps", "unlim-50": "Unlimited Dedicated 50 Mbps",
    "unlim-100": "Unlimited Dedicated 100 Mbps", "unlim-250": "Unlimited Dedicated 250 Mbps",
    "unlim-500": "Unlimited Dedicated 500 Mbps", "unlim-1000": "Unlimited Dedicated 1 Gbps",
    "unlim-2000": "Unlimited Dedicated 2 Gbps", "unlim-5000": "Unlimited Dedicated 5 Gbps",
    "default-50": "Limited 50 Mbps", "default-1000": "Limited 1 Gbps", "default-10000": "Limited 10 Gbps",
}
CURRENCY = {"usd": "$", "eur": "€", "rub": "₽"}


def city_name(code: str) -> str:
    """"Riga-2" → "Рига 2", "amsterdam-2" → "Амстердам 2", "silicon-valley" → "Силиконовая долина"."""
    code = (code or "").strip()
    low = code.lower()
    base, _, num = low.rpartition("-")
    if base and num.isdigit():
        return f"{CITY_RU.get(base, base.replace('-', ' ').title())} {num}"
    return CITY_RU.get(low, code.replace("-", " ").title())


def plan_title(name: str, opt: dict | None = None) -> str:
    if name in PLAN_NAMES:
        return PLAN_NAMES[name]
    return (opt or {}).get("verbose_name") or name.replace("-", " ").title()


def money(amount: float, currency: str = "usd") -> str:
    return f"{CURRENCY.get((currency or 'usd').lower(), '$')}{amount:g}"


async def _get(path: str):
    now = time.time()
    hit = _CACHE.get(path)
    if hit and now - hit[0] < TTL:
        return hit[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(config.hostvds_public_api.rstrip("/") + path,
                         headers={"Accept": "application/json"}) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
    _CACHE[path] = (now, data)
    return data


async def regions() -> dict[str, dict]:
    return {r["region"]: r for r in await _get("/regions/")}


async def plans(region: str) -> list[dict]:
    return await _get(f"/plans/?region={region}")


def network_plans(plan: dict) -> tuple[list[dict], str]:
    """[{name, title, price, bandwidth, unlimited, visible}], default name — sorted like the panel."""
    eo = plan.get("extra_opts") or {}
    opts = eo.get("network_opts") or {}
    default = eo.get("default_network_opt") or "default"
    result = []
    for name, o in opts.items():
        result.append({
            "name": name, "title": plan_title(name, o), "price": float(o.get("monthly_price") or 0),
            "bandwidth": o.get("max_bandwidth"), "unlimited": (o.get("base_traffic") or 0) >= 1_000_000,
            "visible": bool(o.get("visible")) or name == default,
        })
    result.sort(key=lambda p: (p["name"] != default, not p["visible"], p["price"], p["bandwidth"] or 0))
    return result, default
