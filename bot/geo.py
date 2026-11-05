"""Country flags for provider locations, detected by geolocating a public IP of the location."""
import logging
from dataclasses import dataclass

import aiohttp

from . import db
from .config import config

log = logging.getLogger(__name__)

COUNTRY_RU = {
    "NL": "Нидерланды", "DE": "Германия", "FI": "Финляндия", "FR": "Франция", "GB": "Великобритания",
    "US": "США", "HK": "Гонконг", "SG": "Сингапур", "IN": "Индия", "JP": "Япония", "SE": "Швеция",
    "PL": "Польша", "LV": "Латвия", "LT": "Литва", "EE": "Эстония", "RU": "Россия", "KZ": "Казахстан",
    "TR": "Турция", "AE": "ОАЭ", "CA": "Канада", "ES": "Испания", "IT": "Италия", "CH": "Швейцария",
    "AT": "Австрия", "CZ": "Чехия", "RO": "Румыния", "BG": "Болгария", "MD": "Молдова", "UA": "Украина",
    "AM": "Армения", "GE": "Грузия", "IL": "Израиль", "KR": "Южная Корея", "TW": "Тайвань", "AU": "Австралия",
    "BR": "Бразилия", "NO": "Норвегия", "DK": "Дания", "IE": "Ирландия", "BE": "Бельгия", "PT": "Португалия",
}


@dataclass
class Location:
    country: str = ""   # ISO code, e.g. "NL"
    city: str = ""

    @property
    def flag(self) -> str:
        if len(self.country) != 2 or not self.country.isalpha():
            return "🌐"
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in self.country.upper())

    @property
    def title(self) -> str:
        return self.city or COUNTRY_RU.get(self.country, self.country) or "Неизвестно"


def _overrides() -> dict[str, Location]:
    """REGION_NAMES=eu-west1=NL:Amsterdam,us-west=US:Silicon Valley"""
    result = {}
    for item in config.region_names.split(","):
        if "=" not in item:
            continue
        region, value = item.split("=", 1)
        country, _, city = value.partition(":")
        result[region.strip()] = Location(country.strip().upper(), city.strip())
    return result


async def _lookup(ip: str) -> Location | None:
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        try:
            async with s.get(f"https://ipwho.is/{ip}") as r:
                d = await r.json(content_type=None)
                if d.get("success"):
                    return Location(d.get("country_code", ""), d.get("city", ""))
        except Exception as e:
            log.info("ipwho.is failed for %s: %s", ip, e)
        try:
            async with s.get(f"https://api.country.is/{ip}") as r:
                d = await r.json(content_type=None)
                if d.get("country"):
                    return Location(d["country"], "")
        except Exception as e:
            log.info("country.is failed for %s: %s", ip, e)
    return None


async def locate(provider: str, region: str, sample_ip: str | None,
                 known_country: str = "", known_city: str = "") -> Location:
    override = _overrides().get(region)
    if override:
        return override
    if known_country:
        return Location(known_country, known_city)
    cached = await db.get_region_geo(provider, region)
    if cached:
        return Location(*cached)
    if not sample_ip:
        return Location()
    loc = await _lookup(sample_ip)
    if loc and loc.country:
        await db.set_region_geo(provider, region, loc.country, loc.city)
        return loc
    return Location()
