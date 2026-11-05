"""Minimal async client for the HostVDS cloud (OpenStack: Keystone, Nova, Neutron, Glance)."""
import asyncio
import base64
import ipaddress
import secrets
import shlex
import string
import time
from datetime import datetime

import aiohttp
from passlib.hash import sha512_crypt

from . import hostvds_public as public
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, blocks24

COMPUTE_MICROVERSION = "compute 2.47"


HostVDSError = ProviderError

SECURITY_GROUP = "allow_all"   # the group the HostVDS panel attaches to every new server


def make_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def password_user_data(password: str, user: str = "root") -> str:
    """Same script the HostVDS panel sends: set the root password (sha512 hash) once cloud-init has run.
    HostVDS images ignore Nova's adminPass, so without this the password is unknown."""
    pw_hash = sha512_crypt.using(rounds=5000).hash(password)
    script = "\n".join([
        "#!/bin/bash",
        f"((sleep 15; printf %s {shlex.quote(f'{user}:{pw_hash}')} | chpasswd --encrypted) "
        f"&& passwd -u {shlex.quote(user)})&",
    ])
    return base64.b64encode(script.encode()).decode()


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
        self._sg: dict[str, str] = {}

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
                       json: dict | None = None, params: dict | None = None, _retry: bool = True,
                       microversion: str | None = None):
        await self._ensure_token()
        url = path if path.startswith("http") else self._endpoint(service, region) + path
        headers = {"X-Auth-Token": self._token, "Accept": "application/json"}
        if service == "compute":
            headers["OpenStack-API-Version"] = microversion or COMPUTE_MICROVERSION
        async with self._session.request(method, url, json=json, params=params, headers=headers) as r:
            if r.status == 401 and _retry:
                self._token = None
                return await self._request(method, service, region, path, json=json, params=params, _retry=False,
                                           microversion=microversion)
            if r.status >= 400:
                raise HostVDSError(f"{method} {service}{path} → {r.status}: {(await r.text())[:300]}", r.status)
            if r.status == 204 or r.content_length == 0:
                return None
            return await r.json(content_type=None)

    # ---------- resources ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        """Tariffs as sold on hostvds.com (prices, stock, network plans); raw Nova flavors as a fallback."""
        try:
            plans = await public.plans(region)
        except Exception:
            plans = None
        if plans:
            flavors = []
            for p in plans:
                if p.get("is_out_of_stock") or p.get("force_out_of_stock") or not p.get("flavor_uuid"):
                    continue
                cur = p.get("currency") or "usd"
                nets, default = public.network_plans(p)
                flavors.append(Flavor(p["flavor_uuid"], p["name"], p["vcpus"], p["ram"], round(p["disk"] / 1024),
                                      f"{public.money(round(float(p.get('monthly') or 0), 2), cur)}/мес",
                                      p.get("kind", ""), nets, default, cur))
            if flavors:
                return sorted(flavors, key=lambda f: (f.kind != "standard", f.vcpus, f.ram_mb, f.disk_gb))
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
        """Public IPv4 /24 blocks (e.g. 95.182.87.#) available on the region's public networks."""
        nets = (await self._request("GET", "network", region, "/networks"))["networks"]
        subnets = (await self._request("GET", "network", region, "/subnets"))["subnets"]
        usable = {n["id"] for n in nets if n.get("router:external") or n.get("shared")} or {n["id"] for n in nets}
        try:  # free IPv4 per network from the public site API
            info = (await public.regions()).get(region) or {}
            free = {n["network_id"]: n.get("available_ips_v4") for n in info.get("networks") or []}
        except Exception:
            free = {}
        pools: dict[str, IpPool] = {}
        for s in subnets:
            if s.get("ip_version") != 4 or s["network_id"] not in usable:
                continue
            if ipaddress.ip_network(s["cidr"], strict=False).is_private:
                continue
            for prefix in blocks24(s["cidr"], s.get("allocation_pools")):
                pools.setdefault(prefix, IpPool(prefix, s["network_id"], [s["cidr"]], s.get("id"),
                                                free.get(s["network_id"])))
        return sorted(pools.values(), key=lambda p: tuple(int(x) for x in p.prefix.split(".")))

    # ---------- ports: free & fast way to pick an IP before buying a server ----------
    supports_ports = True

    async def security_group(self, region: str) -> str:
        """ID of the "allow_all" security group (created if missing) — without it every inbound
        connection, SSH included, is dropped by the default group."""
        cached = self._sg.get(region)
        if cached:
            return cached
        groups = (await self._request("GET", "network", region, "/security-groups",
                                      params={"name": SECURITY_GROUP}))["security_groups"]
        if groups:
            sg_id = groups[0]["id"]
        else:
            sg_id = (await self._request("POST", "network", region, "/security-groups", json={"security_group": {
                "name": SECURITY_GROUP, "description": "Allow all inbound traffic (created by VPS Buyer)"}}
            ))["security_group"]["id"]
            for ethertype in ("IPv4", "IPv6"):
                try:
                    await self._request("POST", "network", region, "/security-group-rules", json={
                        "security_group_rule": {"security_group_id": sg_id, "direction": "ingress",
                                                "ethertype": ethertype}})
                except HostVDSError as e:
                    if e.status != 409:  # rule already exists
                        raise
        self._sg[region] = sg_id
        return sg_id

    async def create_port(self, region: str, network_id: str, subnet_id: str | None) -> tuple[str, str | None]:
        port: dict = {"network_id": network_id, "name": "ip-hunt",
                      "security_groups": [await self.security_group(region)]}
        if subnet_id:
            port["fixed_ips"] = [{"subnet_id": subnet_id}]
        data = (await self._request("POST", "network", region, "/ports", json={"port": port}))["port"]
        ip = next((f["ip_address"] for f in data.get("fixed_ips", []) if ":" not in f["ip_address"]), None)
        return data["id"], ip

    async def delete_port(self, region: str, port_id: str) -> None:
        try:
            await self._request("DELETE", "network", region, f"/ports/{port_id}")
        except HostVDSError as e:
            if e.status != 404:
                raise

    async def check(self) -> str:
        await self.authenticate()
        return "Локации: " + (", ".join(self.regions()) or "—")

    async def region_statuses(self) -> list[RegionStatus]:
        await self._ensure_token()
        try:
            pub = await public.regions()
        except Exception:
            pub = {}
        if not pub:  # site API unreachable → probe every region through OpenStack
            return list(await asyncio.gather(*(self.region_status(r) for r in self.regions())))
        result = []
        for region in self.regions():
            info = pub.get(region)
            if not info:
                result.append(RegionStatus(region, False, "нет на сайте HostVDS", None))
                continue
            ok, reason = True, ""
            if info.get("is_maintenance"):
                ok, reason = False, "техработы"
            elif not info.get("is_available") or info.get("is_read_only"):
                ok, reason = False, "создание серверов закрыто"
            elif info.get("is_out_of_stock"):
                ok, reason = False, "нет в наличии"
            elif not info.get("available_ips_v4"):
                ok, reason = False, "нет свободных IPv4"
            result.append(RegionStatus(region, ok, reason, None, [], (info.get("country_code") or "").upper(),
                                       public.city_name(info.get("city_code", ""))))
        return result

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
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None) -> Server:
        if port_id:
            networks = [{"port": port_id}]
        else:
            networks = [{"uuid": network_id}] if network_id else "auto"
        password = make_password()
        server: dict = {"name": name, "flavorRef": flavor.id, "networks": networks,
                        "user_data": password_user_data(password)}
        if not port_id:  # a port already carries its security group
            server["security_groups"] = [{"name": await self.security_group(region)}]
        if metadata:  # e.g. {"network_plan": "unlim-shared-1000"} — how HostVDS picks the network plan
            server["metadata"] = {k: str(v) for k, v in metadata.items() if v}
        if flavor.disk_gb:
            server["imageRef"] = image_id
        else:  # flavor without local disk → boot from volume
            server["block_device_mapping_v2"] = [{
                "boot_index": 0, "uuid": image_id, "source_type": "image", "destination_type": "volume",
                "volume_size": boot_volume_gb, "delete_on_termination": True,
            }]
        data = (await self._request("POST", "compute", region, "/servers", json={"server": server}))["server"]
        return Server(data["id"], name, "BUILD", [], password)

    async def repair_access(self, region: str, server_id: str) -> str:
        """For servers bought before the fix: open the firewall (allow_all) and reinstall the same image
        with a known root password. The IP stays the same. Returns the new password."""
        sg_id = await self.security_group(region)
        srv = (await self._request("GET", "compute", region, f"/servers/{server_id}"))["server"]
        names = {g.get("name") for g in srv.get("security_groups") or []}
        if SECURITY_GROUP not in names and sg_id not in names:
            await self._request("POST", "compute", region, f"/servers/{server_id}/action",
                                json={"addSecurityGroup": {"name": sg_id}})
        image_id = (srv.get("image") or {}).get("id") if isinstance(srv.get("image"), dict) else None
        if not image_id:
            raise HostVDSError("Не удалось определить образ сервера для переустановки")
        password = make_password()
        rebuild = {"imageRef": image_id, "user_data": password_user_data(password)}
        if srv.get("metadata"):
            rebuild["metadata"] = srv["metadata"]
        await self._request("POST", "compute", region, f"/servers/{server_id}/action",
                            json={"rebuild": rebuild}, microversion="compute 2.57")
        return password

    async def get_server(self, region: str, server_id: str) -> Server:
        s = (await self._request("GET", "compute", region, f"/servers/{server_id}"))["server"]
        ips = [a["addr"] for addrs in s.get("addresses", {}).values() for a in addrs if a.get("version") == 4]
        return Server(s["id"], s["name"], s["status"], ips)

    async def delete_server(self, region: str, server_id: str) -> None:
        # Ports we created ourselves are not removed by Nova — find them first, clean up after.
        try:
            ports = (await self._request("GET", "network", region, "/ports",
                                         params={"device_id": server_id}))["ports"]
        except Exception:
            ports = []
        try:
            await self._request("DELETE", "compute", region, f"/servers/{server_id}")
        except HostVDSError as e:
            if e.status != 404:
                raise
        for p in ports:
            for _ in range(10):  # port is busy until Nova detaches it
                try:
                    await self.delete_port(region, p["id"])
                    break
                except HostVDSError:
                    await asyncio.sleep(3)

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        """Returns as soon as the server has an IPv4 (often while still BUILD) — lets us reject fast."""
        return await self._wait(region, server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=True)

    async def _wait(self, region: str, server_id: str, timeout: int, need_active: bool) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.ipv4 and (s.status == "ACTIVE" or not need_active):
                return s
            if s.status == "ERROR":
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(2)
