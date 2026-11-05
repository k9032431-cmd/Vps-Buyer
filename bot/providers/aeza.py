"""Aeza — https://my.aeza.net/api (Swagger: https://my.aeza.net/api/docs), header "X-API-Key: <key>".

The tariff catalog (/services/products) is public; locations are product groups labelled
"RU-SHARED", "DE-DEDICATED", … ; a server is an hourly order:
    POST /services/orders {"productId", "count": 1, "term": "hour", "name",
                           "parameters": {"os": <os id>}, "autoProlong": false, "method": "balance"}
The service's public IP is in "ip"; DELETE /services/{id} removes it (the paid hour is not refunded).
Aeza generates the root password itself, so once the server is up the bot sets its own via
/services/{id}/changePassword. Prices in the catalog are in euro cents."""
import asyncio
import logging
import secrets
import string
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, prefix24

API_URL = "https://my.aeza.net/api"
log = logging.getLogger(__name__)

LOCATIONS = {  # group label prefix → (country, city)
    "RU": ("RU", "Москва"), "MSK": ("RU", "Москва"), "SPB": ("RU", "Санкт-Петербург"),
    "DE": ("DE", "Франкфурт"), "FI": ("FI", "Хельсинки"), "NL": ("NL", "Амстердам"), "SE": ("SE", "Стокгольм"),
    "FR": ("FR", "Париж"), "GB": ("GB", "Лондон"), "AT": ("AT", "Вена"), "US": ("US", "США"),
    "WS": ("PL", "Варшава"), "PL": ("PL", "Варшава"),
}
ACTIVE = {"active", "running", "started", "on"}
DEAD = {"deleted", "blocked", "suspended", "error", "failed", "canceled", "cancelled"}


def _password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _money(cents: float) -> str:
    return f"€{cents / 100:.2f}"


class AezaClient:
    def __init__(self, api_key: str, api_url: str = API_URL):
        self.api_key = api_key.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._catalog: list[dict] | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None,
                       base: str | None = None, auth: bool = True):
        headers = {"Accept": "application/json"}
        if auth:  # the public catalog answers 401 to a bad key, so it is fetched without one
            headers["X-API-Key"] = self.api_key
        for attempt in range(4):
            async with self._session.request(method, (base or self.api_url) + path, json=json, params=params,
                                             headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(10)
                    continue
                body = await r.json(content_type=None) if r.status != 204 else None
                err = body.get("error") if isinstance(body, dict) else None
                if r.status >= 400 or err:
                    msg = err.get("message") if isinstance(err, dict) else (err or (body or {}).get("message"))
                    if r.status == 401:
                        msg = f"неверный API-ключ ({msg})"
                    raise ProviderError(f"Aeza {method} {path} → {r.status}: {msg or r.reason}", r.status)
                return body

    @staticmethod
    def _items(body) -> list[dict]:
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        return data if isinstance(data, list) else []

    # ---------- account ----------
    async def check(self) -> str:
        await self._request("GET", "/services", params={"limit": 1})  # validates the key
        try:
            desk = await self._request("GET", "/desktop")
            data = desk.get("data", desk) if isinstance(desk, dict) else {}
            bal = data.get("balance")
            value = bal.get("value") if isinstance(bal, dict) else bal
            if value is not None:
                return f"баланс {_money(float(value))}"
        except Exception:
            pass
        return "ключ API принят"

    # ---------- catalog ----------
    async def _products(self) -> list[dict]:
        """Hourly VPS tariffs with their location code ("RU", "DE", …) and availability."""
        if self._catalog is None:
            v1, v2 = await asyncio.gather(self._request("GET", "/services/products", auth=False),
                                          self._v2_products(), return_exceptions=True)
            if isinstance(v1, BaseException):
                raise v1
            available = v2 if isinstance(v2, dict) else {}
            catalog = []
            for p in self._items(v1):
                if p.get("type") != "vps" or p.get("isPrivate"):
                    continue
                payload = (p.get("group") or {}).get("payload") or {}
                label = payload.get("label") or ""
                hour = (p.get("prices") or {}).get("hour")
                if not label or not hour:
                    continue  # custom builds have no hourly price
                cfg = p.get("summaryConfiguration") or {}
                catalog.append({
                    "id": p["id"], "name": p["name"], "location": label.split("-")[0],
                    "disabled": bool(payload.get("isDisabled")) or available.get(p["id"]) is False,
                    "hour": hour, "month": (p.get("prices") or {}).get("month"),
                    "cpu": (cfg.get("cpu") or {}).get("base", 0), "ram": (cfg.get("ram") or {}).get("base", 0),
                    "rom": (cfg.get("rom") or {}).get("base", 0),
                })
            self._catalog = catalog
        return self._catalog

    async def _v2_products(self) -> dict[int, bool]:
        out, offset = {}, 0
        while True:
            page = await self._request("GET", "/services/products", params={"limit": 100, "offset": offset},
                                       base=self.api_url + "/v2", auth=False)
            items = page.get("items") or []
            out.update({p["id"]: bool(p.get("isAvailable", True)) for p in items})
            offset += len(items)
            if not items or offset >= int(page.get("total", offset)):
                return out

    async def _services(self) -> list[dict]:
        return self._items(await self._request("GET", "/services", params={"limit": 500}))

    async def region_statuses(self) -> list[RegionStatus]:
        products, services = await asyncio.gather(self._products(), self._services())
        regions: dict[str, list[dict]] = {}
        for p in products:
            regions.setdefault(p["location"], []).append(p)
        by_product = {p["id"]: p["location"] for p in products}
        result = []
        for code, items in sorted(regions.items()):
            ok = any(not p["disabled"] for p in items)
            pools = self._pools_from(services, code, by_product)
            country, city = LOCATIONS.get(code, ("", code))
            result.append(RegionStatus(code.lower(), ok, "" if ok else "нет в наличии",
                                       pools[0].cidrs[0] if pools else None, pools, country, city))
        return result

    @staticmethod
    def _pools_from(services: list[dict], code: str, by_product: dict) -> list[IpPool]:
        pools: dict[str, IpPool] = {}
        for s in services:
            pid = s.get("productId") or (s.get("product") or {}).get("id")
            ip = s.get("ip")
            if ip and by_product.get(pid) == code and ":" not in ip:
                pools.setdefault(prefix24(ip), IpPool(prefix24(ip), None, [])).cidrs.append(ip)
        return sorted(pools.values(), key=lambda p: tuple(int(x) for x in p.prefix.split(".")))

    async def ip_pools(self, region: str) -> list[IpPool]:
        products, services = await asyncio.gather(self._products(), self._services())
        return self._pools_from(services, region.upper(), {p["id"]: p["location"] for p in products})

    async def list_flavors(self, region: str) -> list[Flavor]:
        flavors = [Flavor(str(p["id"]), p["name"], int(p["cpu"]), int(p["ram"]) * 1024, int(p["rom"]),
                          f"{_money(p['hour'])}/час" + (f" · {_money(p['month'])}/мес" if p.get("month") else ""))
                   for p in await self._products() if p["location"] == region.upper() and not p["disabled"]]
        hours = {str(p["id"]): p["hour"] for p in await self._products()}
        return sorted(flavors, key=lambda f: (hours[f.id], f.vcpus, f.ram_mb))

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет доступных тарифов")
        hour = next(p["hour"] for p in await self._products() if str(p["id"]) == flavors[0].id)
        return flavors[0], hour / 100

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        items = self._items(await self._request("GET", "/os", params={"limit": 500}))
        images = []
        for o in items:
            if o.get("enabled") is False or o.get("isEnabled") is False:
                continue
            name = o.get("name") or o.get("title") or o.get("slug") or str(o.get("id"))
            if o.get("group") and isinstance(o["group"], str) and o["group"].lower() not in name.lower():
                name = f"{o['group']} {name}"
            images.append(Image(str(o["id"]), name))
        return images

    # ---------- servers ----------
    @staticmethod
    def _server(s: dict) -> Server:
        status = str(s.get("currentStatus") or s.get("status") or "").lower()
        mapped = "ACTIVE" if status in ACTIVE else "ERROR" if status in DEAD else (status.upper() or "BUILD")
        ip = s.get("ip")
        return Server(str(s["id"]), s.get("name", ""), mapped, [ip] if ip and ":" not in ip else [])

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None) -> Server:
        body = await self._request("POST", "/services/orders", {
            "productId": int(flavor.id), "count": 1, "term": "hour", "name": name,
            "parameters": {"os": int(image_id) if str(image_id).isdigit() else image_id, "recipe": None},
            "autoProlong": False, "backups": False, "method": "balance",
        })
        data = body.get("data", body) if isinstance(body, dict) else {}
        ids = (data.get("createdServiceIds") if isinstance(data, dict) else None) or []
        if not ids and isinstance(data, dict):
            ids = [x.get("id") for x in data.get("items") or [] if isinstance(x, dict) and x.get("id")]
        service_id = str(ids[0]) if ids else await self._find_by_name(name)
        return Server(service_id, name, "BUILD", [])

    async def _find_by_name(self, name: str, timeout: int = 180) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for s in await self._services():
                if s.get("name") == name and s.get("id") is not None:
                    return str(s["id"])
            await asyncio.sleep(5)
        raise ProviderError(f"Aeza: заказ «{name}» не превратился в сервер за {timeout} с")

    async def get_server(self, region: str, server_id: str) -> Server:
        items = self._items(await self._request("GET", f"/services/{server_id}"))
        s = next((x for x in items if str(x.get("id")) == str(server_id)), items[0] if items else None)
        if not s:
            raise ProviderError(f"Aeza: сервер {server_id} не найден", 404)
        return self._server(s)

    async def delete_server(self, region: str, server_id: str) -> None:
        for attempt in range(12):
            try:
                await self._request("DELETE", f"/services/{server_id}")
                return
            except ProviderError as e:
                if e.status == 404:
                    return
                if e.status in (400, 409, 423) and attempt < 11:  # still being set up
                    await asyncio.sleep(5)
                    continue
                raise

    async def _set_password(self, server_id: str) -> str | None:
        password = _password()
        last: Exception | None = None
        for attempt in range(5):
            for method in ("POST", "PUT"):
                try:
                    await self._request(method, f"/services/{server_id}/changePassword", {"password": password})
                    return password
                except ProviderError as e:
                    if e.status not in (404, 405):
                        last = e
                        break
            await asyncio.sleep(8)
        log.warning("Aeza: could not set password for %s: %s", server_id, last)
        return None

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(server_id, timeout, need_active=True)

    async def finalize(self, region: str, server_id: str) -> str | None:
        """Called by the purchase loop for every server it keeps: set a root password we know."""
        return await self._set_password(server_id)

    async def _wait(self, server_id: str, timeout: int, need_active: bool) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server("", server_id)
            if s.ipv4 and (s.status == "ACTIVE" or not need_active):
                return s
            if s.status == "ERROR":
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(4)
