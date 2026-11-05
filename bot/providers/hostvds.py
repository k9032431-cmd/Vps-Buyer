"""Minimal async client for the HostVDS cloud (OpenStack: Keystone, Nova, Neutron, Glance)."""
import asyncio
import ipaddress
import time
from datetime import datetime

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server

COMPUTE_MICROVERSION = "compute 2.47"


HostVDSError = ProviderError


class HostVDSClient:
    def __init__(self, auth_url: str, username: str, password: str, project_name: str,
                 user_domain: str = "Default", project_domain_id: str = "default"):
        self.auth_url = auth_url.rstrip("/")
        self.username = username
        self.password = password
        self.project_name = project_name
        self.user_domain = user_domain
        self.project_domain_id = project_domain_id
        self._token: str | None = None
        self._expires: float = 0
        self._catalog: list[dict] = []
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # ---------- auth / catalog ----------
    async def authenticate(self) -> None:
        body = {"auth": {
            "identity": {"methods": ["password"], "password": {"user": {
                "name": self.username, "domain": {"name": self.user_domain}, "password": self.password}}},
            "scope": {"project": {"name": self.project_name, "domain": {"id": self.project_domain_id}}},
        }}
        async with self._session.post(f"{self.auth_url}/v3/auth/tokens", json=body) as r:
            if r.status not in (200, 201):
                raise HostVDSError(f"Ошибка авторизации ({r.status}): {(await r.text())[:200]}", r.status)
            self._token = r.headers["X-Subject-Token"]
            data = await r.json()
        token = data["token"]
        self._catalog = token.get("catalog", [])
        try:
            self._expires = datetime.fromisoformat(token["expires_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            self._expires = time.time() + 3600

    async def _ensure_token(self) -> None:
        if not self._token or time.time() > self._expires - 300:
            await self.authenticate()

    def regions(self) -> list[str]:
        regions = set()
        for svc in self._catalog:
            if svc.get("type") == "compute":
                for ep in svc.get("endpoints", []):
                    if ep.get("interface") == "public":
                        regions.add(ep.get("region_id") or ep.get("region"))
        return sorted(r for r in regions if r)

    def _endpoint(self, service_type: str, region: str) -> str:
        fallback = None
        for svc in self._catalog:
            if svc.get("type") != service_type:
                continue
            for ep in svc.get("endpoints", []):
                if ep.get("interface") != "public":
                    continue
                if (ep.get("region_id") or ep.get("region")) == region:
                    return self._with_version(service_type, ep["url"])
                fallback = fallback or ep["url"]
        if fallback:
            return self._with_version(service_type, fallback)
        raise HostVDSError(f"Сервис {service_type} не найден в каталоге")

    @staticmethod
    def _with_version(service_type: str, url: str) -> str:
        url = url.rstrip("/")
        if service_type == "network" and not url.endswith("/v2.0"):
            url += "/v2.0"
        if service_type == "image" and not url.endswith("/v2"):
            url += "/v2"
        return url

    async def _request(self, method: str, service: str, region: str, path: str, *,
                       json: dict | None = None, params: dict | None = None, _retry: bool = True):
        await self._ensure_token()
        url = path if path.startswith("http") else self._endpoint(service, region) + path
        headers = {"X-Auth-Token": self._token, "Accept": "application/json"}
        if service == "compute":
            headers["OpenStack-API-Version"] = COMPUTE_MICROVERSION
        async with self._session.request(method, url, json=json, params=params, headers=headers) as r:
            if r.status == 401 and _retry:
                self._token = None
                return await self._request(method, service, region, path, json=json, params=params, _retry=False)
            if r.status >= 400:
                raise HostVDSError(f"{method} {service}{path} → {r.status}: {(await r.text())[:300]}", r.status)
            if r.status == 204 or r.content_length == 0:
                return None
            return await r.json(content_type=None)

    # ---------- resources ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        data = await self._request("GET", "compute", region, "/flavors/detail")
        flavors = [Flavor(f["id"], f["name"], f["vcpus"], f["ram"], f.get("disk", 0)) for f in data["flavors"]]
        return sorted(flavors, key=lambda f: (f.vcpus, f.ram_mb, f.disk_gb))

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        images, path = [], "/images"
        params = {"visibility": "public", "status": "active", "limit": 200}
        for _ in range(10):
            data = await self._request("GET", "image", region, path, params=params)
            images += [Image(i["id"], i.get("name") or i["id"]) for i in data.get("images", [])]
            nxt = data.get("next")
            if not nxt:
                break
            # "next" is like "/v2/images?marker=..."
            path, params = nxt.removeprefix("/v2"), None
        return sorted(images, key=lambda i: i.name.lower())

    async def ip_pools(self, region: str) -> list[IpPool]:
        """Public IPv4 subnets grouped by first octet, e.g. 94 / 123 / 207."""
        nets = (await self._request("GET", "network", region, "/networks"))["networks"]
        subnets = (await self._request("GET", "network", region, "/subnets"))["subnets"]
        usable = {n["id"] for n in nets if n.get("router:external") or n.get("shared")} or {n["id"] for n in nets}
        pools: dict[str, IpPool] = {}
        for s in subnets:
            if s.get("ip_version") != 4 or s["network_id"] not in usable:
                continue
            net = ipaddress.ip_network(s["cidr"], strict=False)
            if net.is_private:
                continue
            prefix = s["cidr"].split(".")[0]
            pool = pools.setdefault(prefix, IpPool(prefix, s["network_id"], []))
            pool.cidrs.append(s["cidr"])
        return sorted(pools.values(), key=lambda p: int(p.prefix))

    async def check(self) -> str:
        await self.authenticate()
        return "Локации: " + (", ".join(self.regions()) or "—")

    async def region_statuses(self) -> list[RegionStatus]:
        await self._ensure_token()
        return list(await asyncio.gather(*(self.region_status(r) for r in self.regions())))

    async def region_status(self, region: str) -> RegionStatus:
        """Checks whether servers can actually be ordered in a region."""
        async def safe(coro):
            try:
                return await asyncio.wait_for(coro, 15)
            except Exception as e:
                return e

        flavors, zones, limits, pools = await asyncio.gather(
            safe(self.list_flavors(region)),
            safe(self._request("GET", "compute", region, "/os-availability-zone")),
            safe(self._request("GET", "compute", region, "/limits")),
            safe(self.ip_pools(region)),
        )
        pools = pools if isinstance(pools, list) else []
        sample_ip = None
        if pools:
            net = ipaddress.ip_network(pools[0].cidrs[0], strict=False)
            sample_ip = str(next(net.hosts(), net.network_address))
        st = RegionStatus(region, True, "", sample_ip, pools)

        if isinstance(flavors, Exception):
            st.available, st.reason = False, f"API недоступен ({flavors})"
        elif not flavors:
            st.available, st.reason = False, "нет тарифов"
        elif isinstance(zones, dict):
            info = zones.get("availabilityZoneInfo") or []
            if info and not any(z.get("zoneState", {}).get("available") for z in info):
                st.available, st.reason = False, "зона закрыта"
        if st.available and isinstance(limits, dict):
            a = limits.get("limits", {}).get("absolute", {})
            max_i, used_i = a.get("maxTotalInstances", -1), a.get("totalInstancesUsed", 0)
            max_c, used_c = a.get("maxTotalCores", -1), a.get("totalCoresUsed", 0)
            if 0 <= max_i <= used_i or 0 <= max_c <= used_c:
                st.available, st.reason = False, "исчерпан лимит"
        return st

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int) -> Server:
        server: dict = {
            "name": name,
            "flavorRef": flavor.id,
            "networks": [{"uuid": network_id}] if network_id else "auto",
        }
        if flavor.disk_gb:
            server["imageRef"] = image_id
        else:  # flavor without local disk → boot from volume
            server["block_device_mapping_v2"] = [{
                "boot_index": 0, "uuid": image_id, "source_type": "image", "destination_type": "volume",
                "volume_size": boot_volume_gb, "delete_on_termination": True,
            }]
        data = (await self._request("POST", "compute", region, "/servers", json={"server": server}))["server"]
        return Server(data["id"], name, "BUILD", [], data.get("adminPass"))

    async def get_server(self, region: str, server_id: str) -> Server:
        s = (await self._request("GET", "compute", region, f"/servers/{server_id}"))["server"]
        ips = [a["addr"] for addrs in s.get("addresses", {}).values() for a in addrs if a.get("version") == 4]
        return Server(s["id"], s["name"], s["status"], ips)

    async def delete_server(self, region: str, server_id: str) -> None:
        try:
            await self._request("DELETE", "compute", region, f"/servers/{server_id}")
        except HostVDSError as e:
            if e.status != 404:
                raise

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.status == "ACTIVE" and s.ipv4:
                return s
            if s.status == "ERROR":
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(5)
