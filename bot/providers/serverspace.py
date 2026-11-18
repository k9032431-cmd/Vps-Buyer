"""Serverspace — https://api.serverspace.ru (OpenAPI: https://docs.serverspace.ru/docs/ru/v1/openapi.json).

Auth: header "X-API-KEY". Servers are configurable (cpu / ram_mb / boot volume / public bandwidth) and
created asynchronously: POST /api/v1/servers → task_id → GET /api/v1/tasks/{id} → server_id.
Every NIC reports ip_address together with its mask, so one server is enough to learn the whole public
subnet (e.g. 95.182.80.0/22 → four blocks a.b.c.#). There is no subnet list in the API, so blocks are
learned from servers (yours, bought ones and IP scouting). Extra public NICs are charged a month of IP
at once, so the bot never hunts IPs with NICs — only with whole servers."""
import asyncio
import ipaddress
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, prefix24, ssh_key_script, subnet_blocks

API_URL = "https://api.serverspace.ru"

# Best-effort names for location ids; unknown ids fall back to geolocating an IP of the location.
LOCATIONS = {
    "am": ("NL", "Амстердам"), "nj": ("US", "Нью-Джерси"), "kz": ("KZ", "Алматы"), "ala": ("KZ", "Алматы"),
    "ist": ("TR", "Стамбул"), "tr": ("TR", "Стамбул"), "tas": ("UZ", "Ташкент"), "uz": ("UZ", "Ташкент"),
    "nsk": ("RU", "Новосибирск"), "spb": ("RU", "Санкт-Петербург"), "msk": ("RU", "Москва"),
    "ds": ("RU", "Москва"), "ru": ("RU", "Москва"),
}
PRESETS = [(1, 1024), (1, 2048), (2, 2048), (2, 4096), (4, 8192), (8, 16384)]


def subnet_of(ip: str, mask: int | None) -> str | None:
    if not ip or ":" in ip or not mask:
        return None
    return str(ipaddress.ip_network(f"{ip}/{mask}", strict=False))


class ServerspaceClient:
    def __init__(self, api_key: str, api_url: str = API_URL):
        self.api_key = api_key.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._locations: dict[str, dict] | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None):
        headers = {"X-API-KEY": self.api_key, "Accept": "application/json"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(10)
                    continue
                body = await r.json(content_type=None) if r.content_length != 0 else None
                errors = body.get("errors") if isinstance(body, dict) else None
                if r.status >= 400 or errors:
                    msg = "; ".join(str(e.get("message") or e.get("code") or e) for e in errors or []
                                    if isinstance(e, dict)) or r.reason
                    if r.status == 401:
                        msg = f"неверный API-ключ ({msg})"
                    raise ProviderError(f"Serverspace {method} {path} → {r.status}: {msg}", r.status)
                return body

    # ---------- account ----------
    async def check(self) -> str:
        p = (await self._request("GET", "/api/v1/project"))["project"]
        cur = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get(str(p.get("currency", "")).upper(),
                                                              p.get("currency", ""))
        return f"баланс {p.get('balance', 0):g} {cur}".strip()

    # ---------- locations / subnets ----------
    async def _locs(self) -> dict[str, dict]:
        if self._locations is None:
            data = await self._request("GET", "/api/v1/locations")
            self._locations = {loc["id"]: loc for loc in data.get("locations", [])}
        return self._locations

    async def _servers(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/servers")).get("servers", [])

    @staticmethod
    def _public_nics(server: dict) -> list[dict]:
        return [n for n in server.get("nics") or []
                if n.get("network_type", "PublicShared") == "PublicShared" and n.get("ip_address")
                and ":" not in n["ip_address"]]

    def _pools_from(self, servers: list[dict], region: str) -> list[IpPool]:
        """Every /24 block of every public subnet seen on servers in this location."""
        pools: dict[str, IpPool] = {}
        for s in servers:
            if s.get("location_id") != region:
                continue
            for nic in self._public_nics(s):
                cidr = subnet_of(nic["ip_address"], nic.get("mask"))
                blocks = subnet_blocks(cidr) if cidr else []
                for prefix in (blocks or [prefix24(nic["ip_address"])]):
                    pool = pools.setdefault(prefix, IpPool(prefix, None, []))
                    if cidr and cidr not in pool.cidrs:
                        pool.cidrs.append(cidr)
        return sorted(pools.values(), key=lambda p: tuple(int(x) for x in p.prefix.split(".")))

    async def region_statuses(self) -> list[RegionStatus]:
        locs, servers = await asyncio.gather(self._locs(), self._servers())
        result = []
        for loc_id, loc in sorted(locs.items()):
            ok, reason = True, ""
            if not loc.get("allow_creating_servers", True):
                ok, reason = False, "создание серверов закрыто"
            elif not loc.get("allow_public_network", True):
                ok, reason = False, "нет публичных IP"
            pools = self._pools_from(servers, loc_id)
            sample = next((n["ip_address"] for s in servers if s.get("location_id") == loc_id
                           for n in self._public_nics(s)), None)
            key = next((k for k in sorted(LOCATIONS, key=len, reverse=True) if loc_id.lower().startswith(k)), None)
            country, city = LOCATIONS[key] if key else ("", "")
            result.append(RegionStatus(loc_id, ok, reason, sample, pools, country, city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        return self._pools_from(await self._servers(), region)

    # ---------- configurations / images ----------
    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        data = await self._request("GET", "/api/v1/images")
        return [Image(i["id"], f"{i.get('type', '')} {i.get('os_version', '')}".strip())
                for i in data.get("images", [])
                if i.get("location_id") == region and i.get("architecture", "X64") == "X64"
                and i.get("type") != "Windows"]  # Windows needs a paid licence and a bigger disk

    async def list_flavors(self, region: str) -> list[Flavor]:
        loc = (await self._locs()).get(region) or {}
        cpus = set(loc.get("cpu_quantity_options") or [c for c, _ in PRESETS])
        rams = set(loc.get("ram_size_options") or [r for _, r in PRESETS])
        disk_mb = max(loc.get("system_volume_min") or 0, 10240)
        bw = max(loc.get("bandwidth_min") or 0, 50)
        combos = [(c, r) for c, r in PRESETS if c in cpus and r in rams]
        images = await self.list_images(region)
        image = next((i for i in images if i.name.lower().startswith("ubuntu")), images[0] if images else None)

        async def price(c: int, r: int) -> float | None:
            if not image:
                return None
            try:
                res = await self._request("POST", "/api/v1/servers/price", {
                    "location_id": region, "image_id": image.id, "cpu": c, "ram_mb": r,
                    "volumes": [{"size_mb": disk_mb}], "networks": [{"bandwidth_mbps": bw}]})
                return float(res.get("price"))
            except Exception:
                return None
        prices = await asyncio.gather(*(price(c, r) for c, r in combos))
        flavors = []
        for (c, r), p in zip(combos, prices):
            # id carries the whole configuration: cpu-ram-disk-bandwidth
            flavors.append(Flavor(f"{c}-{r}-{disk_mb}-{bw}", f"{c} vCPU · {r // 1024} GB", c, r,
                                  disk_mb // 1024, f"{p:g} ₽/мес" if p else ""))
        return flavors

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = [f for f in await self.list_flavors(region) if f.price]
        if not flavors:
            raise ProviderError("Не удалось получить цены для этой локации")
        best = min(flavors, key=lambda f: float(f.price.split()[0]))
        return best, round(float(best.price.split()[0]) / 720, 2)  # billed by usage: ≈ monthly / 720

    # ---------- servers ----------
    def _server(self, s: dict) -> Server:
        state = s.get("state", "")
        nics = self._public_nics(s)
        if state == "Blocked":
            status = "ERROR"
        elif state == "Active" and s.get("is_power_on", True):
            status = "ACTIVE"
        else:
            status = state.upper() or "BUILD"
        srv = Server(str(s["id"]), s.get("name", ""), status, [n["ip_address"] for n in nics], s.get("password"))
        srv.cidrs = [c for c in (subnet_of(n["ip_address"], n.get("mask")) for n in nics) if c]
        return srv

    async def _task(self, task_id: str, timeout: int = 600) -> dict:
        deadline = time.time() + timeout
        while True:
            task = (await self._request("GET", f"/api/v1/tasks/{task_id}"))["task"]
            if task.get("server_id") or task.get("is_completed") in ("Completed", "Failed", "Canceled"):
                return task
            if time.time() > deadline:
                raise ProviderError(f"Serverspace: задача {task_id} не завершилась за {timeout} с")
            await asyncio.sleep(3)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        cpu, ram, disk, bw = (int(x) for x in flavor.id.split("-"))
        res = await self._request("POST", "/api/v1/servers", {
            "location_id": region, "image_id": image_id, "cpu": cpu, "ram_mb": ram,
            "volumes": [{"name": "boot", "size_mb": disk}], "networks": [{"bandwidth_mbps": bw}], "name": name,
            **({"server_init_script": ssh_key_script(keys)} if (keys := ([ssh_key] if ssh_key else [])
                                                                  + list(extra_keys or [])) else {}),
        })
        task = await self._task(res["task_id"])
        if not task.get("server_id"):
            raise ProviderError(f"Serverspace: сервер не создан ({task.get('is_completed')})")
        return Server(str(task["server_id"]), name, "BUILD", [])

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/api/v1/servers/{server_id}"))["server"])

    async def delete_server(self, region: str, server_id: str) -> None:
        for attempt in range(12):
            try:
                await self._request("DELETE", f"/api/v1/servers/{server_id}")
                return
            except ProviderError as e:
                if e.status == 404:
                    return
                if e.status in (400, 409, 423) and attempt < 11:  # server is busy (still being created)
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
            if s.status == "ERROR":
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(3)
