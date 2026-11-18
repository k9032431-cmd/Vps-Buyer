"""Vultr — https://api.vultr.com/v2, "Authorization: Bearer <API key>" (fields checked against vultr/govultr).

Subnets: Vultr's geofeed (geofeed.constant.com) lists every subnet by city. Fast mode: a Reserved IP is
created on its own (POST /reserved-ips), its address is known at once, and an instance created with
reserved_ipv4 = its id gets it as the MAIN IP. Wrong reserved IPs are deleted right away."""
import asyncio
import base64
import time

import aiohttp

from ..config import config
from . import geofeed
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, ssh_key_script

API_URL = "https://api.vultr.com/v2"

# region id → cities as written in Vultr's geofeed
GEO_CITIES = {
    "ewr": {"Piscataway"}, "nrt": {"Shinagawa-ku", "Tokyo"}, "ord": {"Elk Grove Village", "Chicago"},
    "ams": {"Amsterdam"}, "dfw": {"Dallas"}, "lax": {"Los Angeles"}, "fra": {"Frankfurt"}, "lhr": {"London"},
    "atl": {"Atlanta"}, "mia": {"Miami"}, "cdg": {"AUBERVILLIERS", "Paris"}, "syd": {"Alexandria", "Sydney"},
    "sgp": {"Singapore"}, "sea": {"Kent", "Seattle"}, "sjc": {"Santa Clara"}, "mad": {"Madrid"},
    "yto": {"Toronto"}, "blr": {"Bangalore"}, "icn": {"Seoul"}, "itm": {"Osaka"}, "waw": {"Warsaw"},
    "tlv": {"Rosh HaAyin", "Tel Aviv"}, "man": {"Swinton", "Manchester"}, "mex": {"Santiago de Queretaro"},
    "sto": {"Spanga", "Stockholm"}, "mel": {"Port Melbourne", "Melbourne"}, "hnl": {"Honolulu"},
    "sao": {"Osasco", "Sao Paulo"}, "scl": {"Santiago"}, "bom": {"Mumbai"}, "jnb": {"Johannesburg"},
    "del": {"Delhi", "New Delhi"},
}


class VultrClient:
    supports_ports = True     # "ports" here are Reserved IPs
    reserves_ips = True
    max_held_ports = 0
    reserve_parallel = 3

    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._regions: dict[str, dict] | None = None
        self._keys: dict[str, str] = {}

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
                if r.status in (429, 503) and attempt < 3:
                    await asyncio.sleep(5)
                    continue
                if r.status == 204:
                    return None
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status >= 400:
                    msg = (body or {}).get("error") if isinstance(body, dict) else None
                    if r.status == 401:
                        msg = f"неверный API-ключ ({msg})"
                    raise ProviderError(f"Vultr {method} {path} → {r.status}: {msg or r.reason}", r.status)
                return body

    async def _all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        items, cursor = [], None
        while True:
            q = {**(params or {}), "per_page": 500, **({"cursor": cursor} if cursor else {})}
            data = await self._request("GET", path, params=q)
            items += data.get(key, [])
            cursor = (((data.get("meta") or {}).get("links") or {}).get("next")) or None
            if not cursor:
                return items

    # ---------- account ----------
    async def check(self) -> str:
        acc = (await self._request("GET", "/account"))["account"]
        bal = -float(acc.get("balance") or 0) - float(acc.get("pending_charges") or 0)
        return f"{acc.get('email') or 'аккаунт'}, баланс ${bal:.2f}"

    # ---------- regions / subnets ----------
    async def _regs(self) -> dict[str, dict]:
        if self._regions is None:
            self._regions = {r["id"]: r for r in await self._all("/regions", "regions")}
        return self._regions

    async def region_statuses(self) -> list[RegionStatus]:
        regs, plans = await asyncio.gather(self._regs(), self._all("/plans", "plans"))
        sold = {loc for p in plans for loc in p.get("locations", [])}
        return [RegionStatus(rid, rid in sold, "" if rid in sold else "нет тарифов", None, [],
                             (r.get("country") or "").upper(), r.get("city", ""))
                for rid, r in sorted(regs.items())]

    async def ip_pools(self, region: str) -> list[IpPool]:
        cities = GEO_CITIES.get(region) or {(await self._regs()).get(region, {}).get("city", "")}
        return [IpPool(c, None, [c]) for c in await geofeed.subnets(config.vultr_geofeed_url, cities)]

    # ---------- plans / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        plans = [p for p in await self._all("/plans", "plans") if region in p.get("locations", [])]
        plans.sort(key=lambda p: p.get("monthly_cost", 0))
        return [Flavor(p["id"], p["id"], int(p.get("vcpu_count") or 0), int(p.get("ram") or 0),
                       int(p.get("disk") or 0),
                       f"${p.get('monthly_cost', 0):g}/мес · ${p.get('hourly_cost') or p.get('monthly_cost', 0) / 730:.3f}/час")
                for p in plans][:30]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        return flavors[0], float(flavors[0].price.split("$")[-1].split("/")[0])

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        oses = await self._all("/os", "os")
        skip = ("windows", "iso", "snapshot", "backup", "application")
        return [Image(str(o["id"]), o["name"]) for o in oses
                if o.get("arch", "x64") == "x64" and (o.get("family") or "").lower() not in skip]

    # ---------- Reserved IPs (fast mode) ----------
    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None) -> tuple[str, str]:
        data = await self._request("POST", "/reserved-ips", {"region": region, "ip_type": "v4",
                                                            "label": "vps-buyer"})
        rip = data["reserved_ip"]
        return rip["id"], rip.get("subnet")

    async def delete_port(self, region: str, port_id: str) -> None:
        for attempt in range(6):
            try:
                await self._request("DELETE", f"/reserved-ips/{port_id}")
                return
            except ProviderError as e:
                if e.status == 404:
                    return
                if attempt == 5:
                    raise
                await asyncio.sleep(5)  # still attached to an instance that is being deleted

    # ---------- instances ----------
    async def _key_id(self, public_key: str) -> str | None:
        if public_key in self._keys:
            return self._keys[public_key]
        body = public_key.split()[1]
        for k in await self._all("/ssh-keys", "ssh_keys"):
            if body in k.get("ssh_key", ""):
                self._keys[public_key] = k["id"]
                return k["id"]
        parts = public_key.split()
        try:
            created = await self._request("POST", "/ssh-keys", {
                "name": f"vps-buyer {parts[2] if len(parts) > 2 else body[-8:]}", "ssh_key": public_key})
            self._keys[public_key] = created["ssh_key"]["id"]
            return self._keys[public_key]
        except ProviderError:
            return None

    @staticmethod
    def _server(i: dict) -> Server:
        ip = i.get("main_ip")
        ready = i.get("status") == "active" and i.get("power_status") == "running"
        return Server(str(i["id"]), i.get("label", ""), "ACTIVE" if ready else "BUILD",
                      [ip] if ip and ip != "0.0.0.0" else [], i.get("default_password") or None)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        ids = [i for i in [await self._key_id(k) for k in keys] if i]
        body = {"region": region, "plan": flavor.id, "os_id": int(image_id), "label": name, "hostname": name,
                "enable_ipv6": False, "backups": "disabled",
                **({"sshkey_id": ids} if ids else {}), **({"reserved_ipv4": port_id} if port_id else {})}
        if keys and len(ids) < len(keys):  # some keys could not be uploaded: install them on first boot
            body["user_data"] = base64.b64encode(ssh_key_script(keys).encode()).decode()
        srv = self._server((await self._request("POST", "/instances", body))["instance"])
        if ssh_key:
            srv.admin_pass = None  # you chose your own key
        return srv

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/instances/{server_id}"))["instance"])

    async def delete_server(self, region: str, server_id: str) -> None:
        try:
            reserved = [r["id"] for r in await self._all("/reserved-ips", "reserved_ips")
                        if str(r.get("instance_id")) == str(server_id)]
        except ProviderError:
            reserved = []
        try:
            await self._request("DELETE", f"/instances/{server_id}")
        except ProviderError as e:
            if e.status != 404:
                raise
        for rid in reserved:  # a reserved IP outlives its instance (and keeps being billed) — remove it
            await self.delete_port(region, rid)

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
