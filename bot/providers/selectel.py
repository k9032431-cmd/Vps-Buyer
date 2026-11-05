"""Async client for Selectel VDS (vds.selectel.ru, formerly Vscale): https://api.vscale.io/v1, header X-Token."""
import asyncio
import secrets
import string
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server

API_URL = "https://api.vscale.io/v1"
# The API allows 12 server-create requests per minute.
CREATE_INTERVAL = 5.5

LOCATIONS = {"spb": ("RU", "Санкт-Петербург"), "msk": ("RU", "Москва")}


def _password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


class SelectelVDSClient:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._create_lock = asyncio.Lock()
        self._last_create = 0.0

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None):
        headers = {"X-Token": self.token, "Content-Type": "application/json;charset=UTF-8"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(15)
                    continue
                if r.status >= 400:
                    msg = r.headers.get("Vscale-Error-Message") or (await r.text())[:200]
                    if msg.lstrip().startswith("<"):  # nginx HTML page
                        msg = r.reason or ""
                    if r.status == 403:
                        msg = f"неверный токен ({msg})" if msg else "неверный токен"
                    raise ProviderError(f"Selectel {method} {path} → {r.status}: {msg}", r.status)
                return await r.json(content_type=None)

    # ---------- account ----------
    async def check(self) -> str:
        info = (await self._request("GET", "/account")).get("info", {})
        text = info.get("email") or info.get("name") or "аккаунт"
        try:
            bal = await self._request("GET", "/billing/balance")
            text += f", баланс {bal.get('summ', bal.get('balance', 0)) / 100:.2f} ₽"
        except ProviderError:
            pass
        return text

    async def _scalets(self) -> list[dict]:
        return await self._request("GET", "/scalets") or []

    # ---------- locations ----------
    async def region_statuses(self) -> list[RegionStatus]:
        locations, scalets = await asyncio.gather(self._request("GET", "/locations"), self._scalets())
        result = []
        for loc in locations:
            region = loc["id"]
            pools = self._pools_from(scalets, region)
            sample = pools[0].cidrs[0] if pools else None
            ok, reason = True, ""
            if not loc.get("active", True):
                ok, reason = False, "локация закрыта"
            elif not loc.get("rplans"):
                ok, reason = False, "нет тарифов"
            country, city = LOCATIONS.get(region.rstrip("0123456789"), ("", ""))
            result.append(RegionStatus(region, ok, reason, sample, pools, country, city or loc.get("description", "")))
        return result

    @staticmethod
    def _pools_from(scalets: list[dict], region: str) -> list[IpPool]:
        """Selectel has no API for IP ranges — use IPs of servers that already exist in the account."""
        pools: dict[str, IpPool] = {}
        for s in scalets:
            ip = (s.get("public_address") or {}).get("address")
            if s.get("location") == region and ip:
                pool = pools.setdefault(ip.split(".")[0], IpPool(ip.split(".")[0], None, []))
                pool.cidrs.append(ip)
        return sorted(pools.values(), key=lambda p: int(p.prefix))

    async def ip_pools(self, region: str) -> list[IpPool]:
        return self._pools_from(await self._scalets(), region)

    # ---------- tariffs / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        rplans, prices = await asyncio.gather(self._request("GET", "/rplans"),
                                              self._request("GET", "/billing/prices"))
        prices = (prices or {}).get("default", {})
        flavors = []
        for p in rplans:
            if region not in p.get("locations", [region]):
                continue
            price = prices.get(p["id"])
            month = price.get("month") if isinstance(price, dict) else None
            flavors.append(Flavor(p["id"], p["id"], p.get("cpus", 0), p.get("memory", 0),
                                  round(p.get("disk", 0) / 1024), f"{month / 100:g} ₽/мес" if month else ""))
        return sorted(flavors, key=lambda f: (f.vcpus, f.ram_mb, f.disk_gb))

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        images = await self._request("GET", "/images")
        return sorted(
            (Image(i["id"], i.get("description") or i["id"]) for i in images
             if i.get("active", True) and region in i.get("locations", [region])
             and (flavor is None or flavor.id in i.get("rplans", [flavor.id]))),
            key=lambda i: i.name.lower())

    # ---------- servers ----------
    @staticmethod
    def _server(data) -> Server:
        if isinstance(data, list):  # the API sometimes wraps a single server in a list
            data = data[0]
        ip = (data.get("public_address") or {}).get("address")
        return Server(str(data["ctid"]), data.get("name", ""), data.get("status", ""), [ip] if ip else [])

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int) -> Server:
        password = _password()
        async with self._create_lock:  # respect 12 creates per minute
            wait = self._last_create + CREATE_INTERVAL - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_create = time.time()
            data = await self._request("POST", "/scalets", {
                "make_from": image_id, "rplan": flavor.id, "do_start": True,
                "name": name, "password": password, "location": region,
            })
        srv = self._server(data)
        srv.admin_pass = password
        return srv

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server(await self._request("GET", f"/scalets/{server_id}"))

    async def delete_server(self, region: str, server_id: str) -> None:
        try:
            await self._request("DELETE", f"/scalets/{server_id}")
        except ProviderError as e:
            if e.status != 404:
                raise

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.status == "started" and s.ipv4:
                s.status = "ACTIVE"
                return s
            if s.status in ("deleted", "billing"):
                s.status = "ERROR"
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(5)
