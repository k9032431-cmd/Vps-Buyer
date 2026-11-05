"""Timeweb Cloud — https://api.timeweb.cloud (OpenAPI: https://timeweb.cloud/api-docs-data/bundle.json).

Auth: "Authorization: Bearer <token>" (panel → API и Terraform). Regions are availability zones
(spb-1, msk-1, ams-1, fra-1, …): servers are created with preset_id + os_id + availability_zone,
the root password comes back in server.root_pass, the IP in server.networks[public].ips[].
There is no subnet API — IP blocks are learned from existing servers and from IP scouting."""
import asyncio
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, prefix24

API_URL = "https://api.timeweb.cloud"

ZONES = {  # zone prefix → (country, city)
    "spb": ("RU", "Санкт-Петербург"), "msk": ("RU", "Москва"), "nsk": ("RU", "Новосибирск"),
    "ams": ("NL", "Амстердам"), "fra": ("DE", "Франкфурт"), "ala": ("KZ", "Алматы"),
    "gdn": ("PL", "Гданьск"), "waw": ("PL", "Варшава"), "nyc": ("US", "Нью-Йорк"),
}
BUSY = {"installing", "software_install", "reinstalling", "turning_on", "configuring", "cloning", "transfer",
        "rebooting", "hard_rebooting"}
DEAD = {"removing", "removed", "blocked", "permanent_blocked", "no_paid"}


class TimewebClient:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._zone_location: dict[str, str] = {}

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
                    msg = (body or {}).get("message") if isinstance(body, dict) else None
                    if isinstance(msg, list):
                        msg = "; ".join(map(str, msg))
                    if r.status in (401, 403) and path.endswith("/finances"):
                        msg = f"неверный токен ({msg})"
                    raise ProviderError(f"Timeweb {method} {path} → {r.status}: {msg or r.reason}", r.status)
                return body

    # ---------- account ----------
    async def check(self) -> str:
        fin = (await self._request("GET", "/api/v1/account/finances"))["finances"]
        cur = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get((fin.get("currency") or "RUB").upper(), "")
        return f"баланс {fin.get('balance', 0):g} {cur}".strip()

    # ---------- locations ----------
    async def _zones(self) -> dict[str, str]:
        if not self._zone_location:
            data = await self._request("GET", "/api/v2/locations")
            for loc in data.get("locations", []):
                for zone in loc.get("availability_zones") or []:
                    self._zone_location[zone] = loc["location"]
        return self._zone_location

    async def _servers(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/servers", params={"limit": 100})).get("servers", [])

    @staticmethod
    def _ipv4(server: dict) -> list[str]:
        return [ip["ip"] for net in server.get("networks") or [] if net.get("type") == "public"
                for ip in net.get("ips") or [] if ip.get("type") == "ipv4" and ip.get("ip")]

    async def region_statuses(self) -> list[RegionStatus]:
        zones, presets, servers = await asyncio.gather(
            self._zones(), self._request("GET", "/api/v1/presets/servers"), self._servers())
        with_presets = {p.get("location") for p in presets.get("server_presets", [])}
        result = []
        for zone, location in sorted(zones.items()):
            ok = location in with_presets
            pools = self._pools_from(servers, zone)
            country, city = ZONES.get(zone.split("-")[0], ("", ""))
            result.append(RegionStatus(zone, ok, "" if ok else "нет тарифов", pools[0].cidrs[0] if pools else None,
                                       pools, country, city))
        return result

    def _pools_from(self, servers: list[dict], zone: str) -> list[IpPool]:
        pools: dict[str, IpPool] = {}
        for s in servers:
            if s.get("availability_zone") != zone:
                continue
            for ip in self._ipv4(s):
                pools.setdefault(prefix24(ip), IpPool(prefix24(ip), None, [])).cidrs.append(ip)
        return sorted(pools.values(), key=lambda p: tuple(int(x) for x in p.prefix.split(".")))

    async def ip_pools(self, region: str) -> list[IpPool]:
        return self._pools_from(await self._servers(), region)

    # ---------- tariffs / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        location = (await self._zones()).get(region)
        presets = (await self._request("GET", "/api/v1/presets/servers")).get("server_presets", [])
        flavors = [Flavor(str(p["id"]), p.get("description_short") or f"{p['cpu']}×{p['ram'] // 1024} GB",
                          int(p.get("cpu") or 0), int(p.get("ram") or 0), round((p.get("disk") or 0) / 1024),
                          f"{p['price']:g} ₽/мес" if p.get("price") else "")
                   for p in presets if p.get("location") == location]
        return sorted(flavors, key=lambda f: (float(f.price.split()[0]) if f.price else 0, f.vcpus, f.ram_mb))

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        best = flavors[0]
        month = float(best.price.split()[0]) if best.price else 0.0
        return best, round(month / 720, 2)  # Timeweb bills by the hour: ≈ monthly / 720

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        data = await self._request("GET", "/api/v1/os/servers")
        return [Image(str(o["id"]), f"{o.get('name', '')} {o.get('version', '')}".strip())
                for o in data.get("servers_os", [])]

    # ---------- servers ----------
    def _server(self, s: dict) -> Server:
        status = s.get("status", "")
        mapped = "ACTIVE" if status == "on" else "ERROR" if status in DEAD else status.upper() or "BUILD"
        return Server(str(s["id"]), s.get("name", ""), mapped, self._ipv4(s), s.get("root_pass") or None)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None) -> Server:
        data = await self._request("POST", "/api/v1/servers", {
            "name": name, "preset_id": int(flavor.id), "os_id": int(image_id),
            "availability_zone": region, "is_ddos_guard": False,
        })
        return self._server(data["server"])

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/api/v1/servers/{server_id}"))["server"])

    async def delete_server(self, region: str, server_id: str) -> None:
        for attempt in range(12):
            try:
                res = await self._request("DELETE", f"/api/v1/servers/{server_id}")
            except ProviderError as e:
                if e.status == 404:
                    return
                if e.status in (409, 423) and attempt < 11:  # still installing — wait and retry
                    await asyncio.sleep(5)
                    continue
                raise
            if isinstance(res, dict) and (res.get("server_delete") or {}).get("hash"):
                raise ProviderError("Timeweb просит код подтверждения удаления. Отключите «Подтверждение "
                                    "удаления сервисов» в настройках аккаунта Timeweb Cloud", 403)
            return

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
            if s.status == "ERROR":
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(3)
