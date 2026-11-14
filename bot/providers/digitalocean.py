"""DigitalOcean — https://api.digitalocean.com/v2, header "Authorization: Bearer <token>".

Subnets: DigitalOcean publishes a geofeed (https://www.digitalocean.com/geo/google.csv) — every public
IPv4 subnet with its city. The bot shows the subnets of the chosen datacenter before buying, you tick
the ones you want, and droplets are created until one gets an IP inside them (wrong ones are deleted).
A droplet reports its IP with the netmask, so the exact subnet of every droplet is known too.
The root password is set with a user_data script (sha512 hash), like on HostVDS."""
import asyncio
import csv
import io
import ipaddress
import secrets
import shlex
import string
import time

import aiohttp
from passlib.hash import sha512_crypt

from ..config import config
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server

API_URL = "https://api.digitalocean.com/v2"

# region slug prefix → (country, city in Russian, geofeed cities that belong to it)
REGIONS = {
    "nyc": ("US", "Нью-Йорк", {"New York", "North Bergen", "Clifton", "Secaucus"}),
    "sfo": ("US", "Сан-Франциско", {"San Francisco", "Santa Clara"}),
    "ams": ("NL", "Амстердам", {"Amsterdam"}),
    "fra": ("DE", "Франкфурт", {"Frankfurt"}),
    "lon": ("GB", "Лондон", {"London"}),
    "sgp": ("SG", "Сингапур", {"Singapore"}),
    "blr": ("IN", "Бангалор", {"Bangalore", "Bengaluru"}),
    "tor": ("CA", "Торонто", {"Toronto"}),
    "syd": ("AU", "Сидней", {"Sydney"}),
    "atl": ("US", "Атланта", {"Atlanta", "Douglasville"}),
    "ric": ("US", "Ричмонд", {"Richmond", "Sandston"}),
}

_GEOFEED: tuple[float, list[tuple[str, str]]] | None = None
GEOFEED_TTL = 24 * 3600


async def geofeed() -> list[tuple[str, str]]:
    """[(cidr, city)] for every public IPv4 subnet, cached for a day."""
    global _GEOFEED
    if _GEOFEED and time.time() - _GEOFEED[0] < GEOFEED_TTL:
        return _GEOFEED[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(config.do_geofeed_url) as r:
            r.raise_for_status()
            text = await r.text()
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or row[0].startswith("#") or ":" in row[0]:
            continue
        try:
            rows.append((str(ipaddress.ip_network(row[0].strip(), strict=False)), row[3].strip()))
        except ValueError:
            continue
    _GEOFEED = (time.time(), rows)
    return rows


def _password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def password_script(password: str) -> str:
    """Set root's password (sha512 hash) once cloud-init is done, and drop DO's "change it at first login"."""
    pw_hash = sha512_crypt.using(rounds=5000).hash(password)
    return "\n".join([
        "#!/bin/bash",
        f"((sleep 15; printf %s {shlex.quote('root:' + pw_hash)} | chpasswd --encrypted; "
        "chage -d \"$(date +%F)\" root; passwd -u root)&",
    ])


class DigitalOceanClient:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

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
                    if r.status == 401:
                        msg = f"неверный токен ({msg})"
                    raise ProviderError(f"DigitalOcean {method} {path} → {r.status}: {msg or r.reason}", r.status)
                return body

    async def _all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        items, page = [], 1
        while True:
            data = await self._request("GET", path, params={**(params or {}), "per_page": 200, "page": page})
            items += data.get(key, [])
            if not (data.get("links") or {}).get("pages", {}).get("next"):
                return items
            page += 1

    # ---------- account ----------
    async def check(self) -> str:
        acc = (await self._request("GET", "/account"))["account"]
        text = acc.get("email") or "аккаунт"
        try:
            bal = await self._request("GET", "/customers/my/balance")
            text += f", баланс ${-float(bal.get('account_balance') or 0):.2f}"
        except ProviderError:
            pass
        if acc.get("status") and acc["status"] != "active":
            text += f" (статус: {acc['status']})"
        return text

    # ---------- regions / subnets ----------
    async def region_statuses(self) -> list[RegionStatus]:
        regions = await self._all("/regions", "regions")
        result = []
        for r in sorted(regions, key=lambda x: x["slug"]):
            country, city, _ = REGIONS.get(r["slug"][:3], ("", r.get("name", ""), set()))
            ok = bool(r.get("available")) and bool(r.get("sizes"))
            result.append(RegionStatus(r["slug"], ok, "" if ok else "создание закрыто", None, [], country, city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        """All public IPv4 subnets of this datacenter from DigitalOcean's geofeed (prefix = CIDR)."""
        cities = REGIONS.get(region[:3], ("", "", set()))[2]
        nets = sorted({cidr for cidr, city in await geofeed() if city in cities},
                      key=lambda c: ipaddress.ip_network(c))
        return [IpPool(c, None, [c]) for c in nets]

    # ---------- sizes / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        sizes = await self._all("/sizes", "sizes")
        flavors = [Flavor(s["slug"], s["slug"], s["vcpus"], s["memory"], s["disk"],
                          f"${s['price_monthly']:g}/мес · ${s['price_hourly']:.3f}/час")
                   for s in sorted(sizes, key=lambda s: (s["price_monthly"], s["slug"]))
                   if s.get("available") and region in s.get("regions", [])]
        return flavors[:30]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        hourly = float(flavors[0].price.split("$")[-1].split("/")[0])
        return flavors[0], hourly

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        images = await self._all("/images", "images", {"type": "distribution"})
        return [Image(i["slug"], f"{i.get('distribution', '')} {i.get('name', '')}".strip())
                for i in images if i.get("slug") and i.get("status", "available") == "available"
                and region in i.get("regions", [region])]

    # ---------- droplets ----------
    @staticmethod
    def _server(d: dict) -> Server:
        v4 = [n for n in (d.get("networks") or {}).get("v4", []) if n.get("type") == "public"]
        status = {"active": "ACTIVE", "new": "BUILD", "off": "ACTIVE", "archive": "ERROR"}.get(d.get("status"), "BUILD")
        srv = Server(str(d["id"]), d.get("name", ""), status, [n["ip_address"] for n in v4])
        srv.cidrs = [str(ipaddress.ip_network(f"{n['ip_address']}/{n['netmask']}", strict=False))
                     for n in v4 if n.get("netmask")]
        return srv

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None) -> Server:
        password = _password()
        data = await self._request("POST", "/droplets", {
            "name": name, "region": region, "size": flavor.id, "image": image_id,
            "user_data": password_script(password), "ipv6": False, "monitoring": False,
            "tags": ["vps-buyer"],
        })
        srv = self._server(data["droplet"])
        srv.admin_pass = password
        return srv

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/droplets/{server_id}"))["droplet"])

    async def delete_server(self, region: str, server_id: str) -> None:
        for attempt in range(12):
            try:
                await self._request("DELETE", f"/droplets/{server_id}")
                return
            except ProviderError as e:
                if e.status == 404:
                    return
                if e.status in (409, 422) and attempt < 11:  # droplet still being created
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
