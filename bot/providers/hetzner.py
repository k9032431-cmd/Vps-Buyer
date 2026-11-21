"""Hetzner Cloud — https://api.hetzner.cloud/v1, "Authorization: Bearer <token>"
(OpenAPI: https://docs.hetzner.cloud/cloud.spec.json).

Subnets: Hetzner's geofeed lists every subnet by city. Fast mode: a Primary IP can be created on its own
(POST /primary_ips, location + assignee_type "server"), its address is known at once, and a server is
created with exactly that IP (public_net.ipv4 = primary IP id). Wrong IPs are deleted right away — far
cheaper than buying servers. The root password comes back in the create response (when no SSH key)."""
import asyncio
import time

import aiohttp

from ..config import config
from . import geofeed
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, merge_scripts, ssh_key_script
from .hostvds import make_password, password_script

API_URL = "https://api.hetzner.cloud/v1"
CITY_RU = {"Falkenstein": "Фалькенштайн", "Nuremberg": "Нюрнберг", "Helsinki": "Хельсинки",
           "Ashburn": "Эшберн", "Hillsboro": "Хиллсборо", "Singapore": "Сингапур"}


class HetznerClient:
    supports_ports = True     # "ports" here are Primary IPs: reserve an IP first, then build on it
    reserves_ips = True
    max_held_ports = 0        # project limits on Primary IPs are low: release wrong ones at once
    reserve_parallel = 3

    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._locations: dict[str, dict] | None = None
        self._keys: dict[str, int] = {}

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
                if r.status == 204:
                    return None
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status >= 400:
                    err = (body or {}).get("error") or {}
                    msg = err.get("message") or r.reason
                    if r.status == 401:
                        msg = f"неверный токен ({msg})"
                    raise ProviderError(f"Hetzner {method} {path} → {r.status}: {msg}", r.status)
                return body

    async def _all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        items, page = [], 1
        while True:
            data = await self._request("GET", path, params={**(params or {}), "per_page": 50, "page": page})
            items += data.get(key, [])
            nxt = ((data.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not nxt:
                return items
            page = nxt

    # ---------- account ----------
    async def check(self) -> str:
        locs = await self._request("GET", "/locations")
        return f"проект доступен, локаций: {len(locs.get('locations', []))}"

    # ---------- locations / subnets ----------
    async def _locs(self) -> dict[str, dict]:
        if self._locations is None:
            self._locations = {l["name"]: l for l in (await self._request("GET", "/locations"))["locations"]}
        return self._locations

    @staticmethod
    def _city(loc: dict) -> str:
        return (loc.get("city") or "").split(",")[0].strip()

    async def region_statuses(self) -> list[RegionStatus]:
        locs, types = await asyncio.gather(self._locs(), self._all("/server_types", "server_types"))
        sold = {p["location"] for t in types if not t.get("deprecated") for p in t.get("prices", [])}
        result = []
        for name, loc in sorted(locs.items()):
            city = self._city(loc)
            ok = name in sold
            result.append(RegionStatus(name, ok, "" if ok else "нет тарифов", None, [],
                                       (loc.get("country") or "").upper(), CITY_RU.get(city, city)))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        loc = (await self._locs()).get(region) or {}
        nets = await geofeed.subnets(config.hetzner_geofeed_url, {self._city(loc)})
        return [IpPool(c, None, [c]) for c in nets]

    # ---------- types / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        flavors = []
        for t in await self._all("/server_types", "server_types"):
            price = next((p for p in t.get("prices", []) if p["location"] == region), None)
            if not price or t.get("deprecated"):
                continue
            month = float(price["price_monthly"]["gross"])
            hour = float(price["price_hourly"]["gross"])
            flavors.append(Flavor(t["name"], t["name"], int(t["cores"]), int(float(t["memory"]) * 1024),
                                  int(t["disk"]), f"€{month:.2f}/мес · €{hour:.4f}/час"))
        return sorted(flavors, key=lambda f: float(f.price.split("€")[1].split("/")[0]))

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        return flavors[0], float(flavors[0].price.split("€")[-1].split("/")[0])

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        images = await self._all("/images", "images", {"type": "system", "status": "available"})
        arch = "arm" if flavor and flavor.id.startswith("cax") else "x86"
        return [Image(str(i["id"]), i.get("description") or i["name"]) for i in images
                if i.get("architecture", "x86") == arch and not i.get("deprecated")]

    # ---------- Primary IPs (fast mode) ----------
    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None) -> tuple[str, str]:
        data = await self._request("POST", "/primary_ips", {
            "name": f"vps-buyer-{int(time.time() * 1000) % 10**10}", "type": "ipv4", "location": region,
            "assignee_type": "server", "auto_delete": True})
        ip = data["primary_ip"]
        return str(ip["id"]), ip["ip"]

    async def delete_port(self, region: str, port_id: str) -> None:
        try:
            await self._request("DELETE", f"/primary_ips/{port_id}")
        except ProviderError as e:
            if e.status != 404:
                raise

    # ---------- servers ----------
    async def _key_id(self, public_key: str) -> int | None:
        if public_key in self._keys:
            return self._keys[public_key]
        body = public_key.split()[1]
        for k in await self._all("/ssh_keys", "ssh_keys"):
            if body in k.get("public_key", ""):
                self._keys[public_key] = k["id"]
                return k["id"]
        parts = public_key.split()
        try:
            created = await self._request("POST", "/ssh_keys", {
                "name": f"vps-buyer {parts[2] if len(parts) > 2 else body[-8:]}", "public_key": public_key})
            self._keys[public_key] = created["ssh_key"]["id"]
            return self._keys[public_key]
        except ProviderError:
            return None

    @staticmethod
    def _server(s: dict) -> Server:
        v4 = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip")
        status = {"running": "ACTIVE", "off": "ACTIVE"}.get(s.get("status"), "BUILD")
        return Server(str(s["id"]), s.get("name", ""), status, [v4] if v4 else [])

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        password = None if ssh_key else make_password()
        body = {"name": name, "server_type": flavor.id, "image": int(image_id) if image_id.isdigit() else image_id,
                "location": region, "start_after_create": True,
                "public_net": {"enable_ipv4": True, "enable_ipv6": False,
                               **({"ipv4": int(port_id)} if port_id else {})}}
        ids = [i for i in [await self._key_id(k) for k in keys] if i]
        if ids:
            body["ssh_keys"] = ids
        # The password is ours (set on first boot): with any SSH key in the request Hetzner sets none.
        # Keys that could not be uploaded are installed by the same script.
        script = merge_scripts(password_script(password) if password else None,
                               ssh_key_script(keys) if keys and not ids else None)
        if password or (keys and not ids):
            body["user_data"] = script
        data = await self._request("POST", "/servers", body)
        srv = self._server(data["server"])
        srv.admin_pass = password
        return srv

    async def list_servers(self) -> list[Server]:
        result = []
        for s in await self._all("/servers", "servers"):
            srv = self._server(s)
            srv.region = (((s.get("datacenter") or {}).get("location")) or {}).get("name", "")
            result.append(srv)
        return result

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/servers/{server_id}"))["server"])

    async def delete_server(self, region: str, server_id: str) -> None:
        for attempt in range(12):
            try:
                await self._request("DELETE", f"/servers/{server_id}")  # its Primary IP goes too (auto_delete)
                return
            except ProviderError as e:
                if e.status == 404:
                    return
                if e.status in (409, 423) and attempt < 11:
                    await asyncio.sleep(5)
                    continue
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
