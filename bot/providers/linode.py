"""Linode (Akamai) — https://api.linode.com/v4, "Authorization: Bearer <token>"
(https://techdocs.akamai.com/linode-api/reference).

Subnets: Linode's geofeed (geoip.linode.com) lists every subnet by city. A new Linode reports its IPv4
right in the create response, so a wrong one is deleted without waiting for the boot."""
import asyncio
import secrets
import string
import time

import aiohttp

from ..config import config
from . import geofeed
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server

API_URL = "https://api.linode.com/v4"

# region id → cities as written in Linode's geofeed (the region label city is used otherwise)
GEO_CITIES = {
    "us-east": {"Cedar Knolls", "Newark"}, "us-central": {"Richardson", "Dallas"}, "us-west": {"Fremont"},
    "us-southeast": {"Atlanta"}, "ca-central": {"Toronto"}, "eu-west": {"London"}, "eu-central": {"Frankfurt"},
    "ap-south": {"Singapore"}, "ap-northeast": {"Tokyo"}, "ap-west": {"Mumbai"}, "ap-southeast": {"Sydney"},
    "us-iad": {"Ashburn", "Washington"}, "us-ord": {"Chicago"}, "fr-par": {"Paris"}, "us-sea": {"Tukwila", "Seattle"},
    "br-gru": {"Sao Paulo"}, "nl-ams": {"Amsterdam"}, "se-sto": {"Stockholm"}, "es-mad": {"Madrid"},
    "in-maa": {"Chennai"}, "jp-osa": {"Osaka"}, "it-mil": {"Milan"}, "us-mia": {"Miami"}, "id-cgk": {"Jakarta"},
    "us-lax": {"Los Angeles"}, "gb-lon": {"London"}, "au-mel": {"Melbourne"}, "in-bom-2": {"Mumbai"},
    "de-fra-2": {"Frankfurt"}, "sg-sin-2": {"Singapore"}, "jp-tyo-3": {"Tokyo"}, "no-osl-1": {"Oslo"},
}


def root_password() -> str:
    """Linode checks password strength: long, mixed, with symbols."""
    alphabet = string.ascii_letters + string.digits + "-_.@#%+="
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(not c.isalnum() for c in pw)):
            return pw


class LinodeClient:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._regions: dict[str, dict] | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, params=params,
                                             headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(10)
                    continue
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status >= 400:
                    errs = (body or {}).get("errors") if isinstance(body, dict) else None
                    msg = "; ".join(e.get("reason", "") for e in errs or []) or r.reason
                    if r.status == 401:
                        msg = f"неверный токен ({msg})"
                    raise ProviderError(f"Linode {method} {path} → {r.status}: {msg}", r.status)
                return body

    async def _all(self, path: str) -> list[dict]:
        items, page = [], 1
        while True:
            data = await self._request("GET", path, params={"page": page, "page_size": 500})
            items += data.get("data", [])
            if page >= int(data.get("pages") or 1):
                return items
            page += 1

    # ---------- account ----------
    async def check(self) -> str:
        acc = await self._request("GET", "/account")
        bal = -float(acc.get("balance") or 0)
        return f"{acc.get('email') or 'аккаунт'}, баланс ${bal:.2f}"

    # ---------- regions / subnets ----------
    async def _regs(self) -> dict[str, dict]:
        if self._regions is None:
            self._regions = {r["id"]: r for r in await self._all("/regions")}
        return self._regions

    async def region_statuses(self) -> list[RegionStatus]:
        result = []
        for rid, r in sorted((await self._regs()).items()):
            if r.get("site_type", "core") != "core":
                continue
            ok = r.get("status") == "ok" and "Linodes" in (r.get("capabilities") or ["Linodes"])
            city = (r.get("label") or rid).split(",")[0].strip()
            result.append(RegionStatus(rid, ok, "" if ok else "недоступна", None, [],
                                       (r.get("country") or "").upper(), city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        label = (await self._regs()).get(region, {}).get("label", "")
        cities = GEO_CITIES.get(region) or {label.split(",")[0].strip()}
        return [IpPool(c, None, [c]) for c in await geofeed.subnets(config.linode_geofeed_url, cities)]

    # ---------- types / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        flavors = []
        for t in await self._all("/linode/types"):
            if t.get("class") in ("gpu", "accelerated"):
                continue
            price = next((p for p in t.get("region_prices") or [] if p.get("id") == region), None) or t["price"]
            month, hour = price.get("monthly"), price.get("hourly") or 0
            flavors.append(Flavor(t["id"], t.get("label") or t["id"], int(t["vcpus"]), int(t["memory"]),
                                  int(t["disk"]) // 1024,
                                  (f"${month:g}/мес · " if month else "") + f"${hour:.3f}/час"))
        return sorted(flavors, key=lambda f: float(f.price.split("$")[-1].split("/")[0]))[:30]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("Нет тарифов")
        return flavors[0], float(flavors[0].price.split("$")[-1].split("/")[0])

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        return [Image(i["id"], i.get("label") or i["id"]) for i in await self._all("/images")
                if i.get("is_public") and i["id"].startswith("linode/") and not i.get("deprecated")
                and i.get("status", "available") == "available"]

    # ---------- instances ----------
    @staticmethod
    def _server(s: dict) -> Server:
        v4 = s.get("ipv4") or []
        ips = [v4] if isinstance(v4, str) else [ip for ip in v4 if not ip.startswith(("10.", "192.168."))]
        return Server(str(s["id"]), s.get("label", ""), "ACTIVE" if s.get("status") == "running" else "BUILD", ips)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        password = None if ssh_key else root_password()
        body = {"type": flavor.id, "region": region, "image": image_id, "label": name, "booted": True,
                **({"root_pass": password} if password else {}), **({"authorized_keys": keys} if keys else {})}
        srv = self._server(await self._request("POST", "/linode/instances", body))
        srv.admin_pass = password
        return srv

    async def list_servers(self) -> list[Server]:
        result = []
        for s in await self._all("/linode/instances"):
            srv = self._server(s)
            srv.region = s.get("region", "")
            result.append(srv)
        return result

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server(await self._request("GET", f"/linode/instances/{server_id}"))

    async def delete_server(self, region: str, server_id: str) -> None:
        try:
            await self._request("DELETE", f"/linode/instances/{server_id}")
        except ProviderError as e:
            if e.status != 404:
                raise

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(server_id, timeout, need_active=True)

    async def _wait(self, server_id: str, timeout: int, need_active: bool) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server("", server_id)
            if s.ipv4 and (s.status == "ACTIVE" or not need_active):
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(3)
