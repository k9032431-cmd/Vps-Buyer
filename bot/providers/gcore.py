"""Gcore Cloud — https://api.gcore.com/cloud, "Authorization: APIKey <permanent API token>" (fields checked
against the official gcore-python SDK). Pay-as-you-go from the prepaid balance.

Fast mode: a public reserved fixed IP is created on its own (reserved_fixed_ips, type "external"), its address
is known once the task finishes, and a server is created on it (interface type "reserved_fixed_ip") — it is the
server's own public IP. Wrong IPs are deleted right away (a reserved IP is billed until deleted).

Gcore's default security group lets in only SSH/RDP/ICMP, so servers get our allow-all group. The root
password and keys come from the first-boot script (Gcore ignores user_data when a password is passed)."""
import asyncio
import base64
import ipaddress
import logging
import time

import aiohttp

from .base import (Flavor, Image, IpPool, ProviderError, RegionStatus, Server, blocks24, merge_scripts,
                   open_access_script, ssh_key_script)
from .hostvds import make_password, password_script

log = logging.getLogger(__name__)
API_URL = "https://api.gcore.com/cloud"
SECURITY_GROUP = "vps-buyer-allow-all"
TASK_TIMEOUT = 300
RELEASE_TIMEOUT = 300
_PENDING: set[asyncio.Task] = set()


async def wait_pending(timeout: float | None = None) -> None:
    if _PENDING:
        await asyncio.wait(set(_PENDING), timeout=timeout)


class GcoreClient:
    supports_ports = True     # "ports" here are reserved public IPs
    reserves_ips = True
    max_held_ports = 0
    reserve_parallel = 3

    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip().removeprefix("APIKey ").strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self.project: int | None = None
        self._regions: dict[str, dict] = {}
        self._sg: dict[str, str | None] = {}
        self._region_of: dict[str, str] = {}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        headers = {"Authorization": f"APIKey {self.token}", "Accept": "application/json"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, params=params,
                                             headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(5)
                    continue
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status < 400:
                    return body
                msg = ((body or {}).get("message") or (body or {}).get("detail") or r.reason) \
                    if isinstance(body, dict) else r.reason
                raise self._error(f"Gcore {method} {path.split('?')[0]} → {r.status}: {msg}", r.status, str(msg), path)
        raise ProviderError(f"Gcore {method} {path}: слишком много запросов", 429)

    @staticmethod
    def _error(text: str, status: int, msg: str, path: str) -> ProviderError:
        low = msg.lower()
        if status in (401, 403) and ("token" in low or "auth" in low or status == 401):
            return ProviderError(text.replace(msg, f"неверный API-токен ({msg})"), 401)
        if "quota" in low or "limit" in low:
            return ProviderError(text, 429)  # out of servers/CPUs: our deletions free it up — wait
        if "balance" in low or "insufficient" in low:
            return ProviderError(text + " — пополните баланс Gcore", 402)
        return ProviderError(text, status)

    async def _task(self, data: dict, timeout: int = TASK_TIMEOUT) -> dict:
        """Waits for the task a POST/DELETE returned; returns its created_resources."""
        tasks = (data or {}).get("tasks") or []
        if not tasks:
            return {}
        deadline = time.time() + timeout
        while True:
            t = await self._request("GET", f"/v1/tasks/{tasks[0]}")
            if t.get("state") == "FINISHED":
                return t.get("created_resources") or {}
            if t.get("state") == "ERROR":
                err = t.get("error") or "ошибка задачи"
                raise self._error(f"Gcore {t.get('task_type', 'task')}: {err}", 400, str(err), "")
            if time.time() > deadline:
                raise ProviderError(f"Gcore: задача {tasks[0]} не завершилась", 429)
            await asyncio.sleep(2)

    # ---------- account ----------
    async def _project(self) -> int:
        if self.project is None:
            projects = (await self._request("GET", "/v1/projects"))["results"]
            if not projects:
                raise ProviderError("Gcore: в аккаунте нет проектов")
            p = next((p for p in projects if p.get("is_default")), projects[0])
            self.project = int(p["id"])
        return self.project

    async def check(self) -> str:
        await self._project()
        return f"проект {self.project}"

    # ---------- regions ----------
    async def _region_list(self) -> dict[str, dict]:
        if not self._regions:
            data = await self._request("GET", "/v1/regions", params={"limit": 1000})
            self._regions = {str(r["id"]): r for r in data.get("results", [])}
        return self._regions

    async def region_statuses(self) -> list[RegionStatus]:
        await self._project()
        result = []
        for rid, r in sorted((await self._region_list()).items(), key=lambda kv: kv[1].get("display_name", "")):
            if not r.get("has_kvm"):
                continue  # no virtual machines there (bare metal / basic VMs / AI only)
            ok = r.get("state") == "ACTIVE"
            city = r.get("display_name") or r.get("slug") or rid
            if r.get("access_level"):
                city += f" · {r['access_level'].capitalize()}"
            result.append(RegionStatus(rid, ok, "" if ok else (r.get("state") or "").lower(), None, [],
                                       (r.get("country") or "").upper(), city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        """Public subnets of the region's external network, when Gcore shows them."""
        p = await self._project()
        try:
            data = await self._request("GET", f"/v1/subnets/{p}/{region}", params={"limit": 1000})
        except ProviderError:
            return []
        ext = (await self._region_list()).get(region, {}).get("external_network_id")
        pools: dict[str, IpPool] = {}
        for s in data.get("results", []):
            if ext and s.get("network_id") != ext:
                continue
            try:
                net = ipaddress.ip_network(s.get("cidr", ""), strict=False)
            except ValueError:
                continue
            if net.version != 4 or not net.is_global or net.prefixlen < 16:
                continue
            for prefix in blocks24(s["cidr"]):
                pools.setdefault(prefix, IpPool(prefix, None, [s["cidr"]]))
        return sorted(pools.values(), key=lambda x: tuple(int(o) for o in x.prefix.split(".")))

    # ---------- flavors / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        p = await self._project()
        data = await self._request("GET", f"/v1/flavors/{p}/{region}", params={
            "include_prices": "true", "disabled": "false", "exclude_windows": "true"})
        flavors = []
        for f in data.get("results", []):
            name = f.get("flavor_name") or f.get("flavor_id")
            if f.get("disabled") or "shared" in name or (f.get("architecture") or "x86_64") not in ("x86_64", "x86"):
                continue  # basic (shared) VMs are created differently; ARM needs ARM images
            hour, month, cur = f.get("price_per_hour"), f.get("price_per_month"), f.get("currency_code") or ""
            price = f"{month:.2f} {cur}/мес · {hour:.4f}/час" if hour is not None and month is not None else ""
            flavors.append(Flavor(f.get("flavor_id") or name, name, int(f.get("vcpus") or 0), int(f.get("ram") or 0),
                                  0, price, currency=cur))
        flavors.sort(key=lambda fl: (float(fl.price.split("·")[1].split("/")[0]) if "·" in fl.price else 99,
                                     fl.vcpus, fl.ram_mb))
        return flavors[:25]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        f = flavors[0]
        return f, float(f.price.split("·")[1].split("/")[0]) if "·" in f.price else 0.01

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        p = await self._project()
        data = await self._request("GET", f"/v1/images/{p}/{region}",
                                   params={"visibility": "public", "architecture": "x86_64"})
        return [Image(i["id"], i.get("name") or f"{i.get('os_distro')} {i.get('os_version')}")
                for i in data.get("results", [])
                if (i.get("os_type") or "linux") == "linux" and (i.get("status") or "active") == "active"]

    # ---------- reserved public IPs (fast mode) ----------
    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None) -> tuple[str, str]:
        p = await self._project()
        try:
            res = await self._task(await self._request("POST", f"/v1/reserved_fixed_ips/{p}/{region}", {
                "type": "external", "ip_family": "ipv4", "is_vip": False}))
        except ProviderError as e:
            if e.status == 429 and any(w in str(e).lower() for w in ("quota", "limit")):
                raise ProviderError(str(e), 403) from e  # out of public IPs: the purchase goes on with servers
            raise
        port_id = (res.get("ports") or [None])[0]
        if not port_id:
            raise ProviderError("Gcore: IP не выдан")
        ip = (await self._request("GET", f"/v1/reserved_fixed_ips/{p}/{region}/{port_id}")).get("fixed_ip_address")
        return port_id, ip

    async def delete_port(self, region: str, port_id: str) -> None:
        p = await self._project()
        try:
            await self._request("DELETE", f"/v1/reserved_fixed_ips/{p}/{region}/{port_id}")
        except ProviderError as e:
            if e.status != 404:
                raise

    # ---------- servers ----------
    async def _security_group(self, region: str) -> str | None:
        if region in self._sg:
            return self._sg[region]
        p = await self._project()
        sg = None
        try:
            groups = (await self._request("GET", f"/v1/securitygroups/{p}/{region}")).get("results", [])
            sg = next((g["id"] for g in groups if g.get("name") == SECURITY_GROUP), None)
            if not sg:
                rules = [{"direction": "ingress", "ethertype": eth, "protocol": "any"} for eth in ("IPv4", "IPv6")]
                rules += [{"direction": "egress", "ethertype": eth, "protocol": "any"} for eth in ("IPv4", "IPv6")]
                sg = (await self._request("POST", f"/v1/securitygroups/{p}/{region}", {"security_group": {
                    "name": SECURITY_GROUP, "description": "VPS Buyer: all ports open",
                    "security_group_rules": rules}}))["id"]
        except ProviderError as e:  # e.g. quota: the default group (SSH only) + ufw off on the server
            log.warning("Gcore %s: allow-all security group unavailable: %s", region, e)
        self._sg[region] = sg
        return sg

    def _server(self, i: dict, region: str = "") -> Server:
        ips = [a["ip_address"] for a in i.get("fixed_ip_assignments") or []
               if a.get("external") and ":" not in a.get("ip_address", "")]
        if not ips:
            for addrs in (i.get("addresses") or {}).values():
                for a in addrs:
                    addr = a.get("addr", "")
                    if ":" not in addr and addr and ipaddress.ip_address(addr).is_global:
                        ips.append(addr)
        status = i.get("status", "")
        mapped = "ACTIVE" if status == "ACTIVE" else "ERROR" if status in ("ERROR", "DELETED", "SHUTOFF") else "BUILD"
        region = region or str(i.get("region_id") or "")
        if region:
            self._region_of[i["instance_id"]] = region
        return Server(i["instance_id"], i.get("instance_name", ""), mapped, ips, region=region)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        p = await self._project()
        password = None if ssh_key else make_password()
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        script = merge_scripts(password_script(password) if password else open_access_script(False),
                               ssh_key_script(keys) if keys else None)
        sg = await self._security_group(region)
        iface = {"type": "reserved_fixed_ip", "port_id": port_id} if port_id else {"type": "external", "ip_family": "ipv4"}
        if sg:
            iface["security_groups"] = [{"id": sg}]
        types = (await self._region_list()).get(region, {}).get("available_volume_types") or ["standard"]
        body = {
            "name": name, "flavor": flavor.id, "interfaces": [iface],
            "volumes": [{"source": "image", "image_id": image_id, "size": max(10, boot_volume_gb), "boot_index": 0,
                         "type_name": "standard" if "standard" in types else types[0],
                         "delete_on_termination": True}],
            "user_data": base64.b64encode(script.encode()).decode(),
        }
        res = await self._task(await self._request("POST", f"/v2/instances/{p}/{region}", body))
        iid = (res.get("instances") or [None])[0]
        if not iid:
            raise ProviderError("Gcore: сервер не создан")
        self._region_of[iid] = region
        return Server(iid, name, "BUILD", [], password, region=region)

    async def get_server(self, region: str, server_id: str) -> Server:
        p = await self._project()
        region = region or self._region_of.get(server_id, "")
        return self._server(await self._request("GET", f"/v1/instances/{p}/{region}/{server_id}"), region)

    async def list_servers(self) -> list[Server]:
        p = await self._project()
        regions = [rid for rid, r in (await self._region_list()).items() if r.get("has_kvm") and r.get("state") == "ACTIVE"]

        async def one(rid: str):
            try:
                data = await self._request("GET", f"/v1/instances/{p}/{rid}", params={"limit": 1000})
            except ProviderError:
                return []
            return [self._server(i, rid) for i in data.get("results", [])]
        return [s for chunk in await asyncio.gather(*(one(r) for r in regions)) for s in chunk]

    async def delete_server(self, region: str, server_id: str) -> None:
        """Deletes the server with its disk and its reserved IP (a reserved IP outlives the server otherwise)."""
        p = await self._project()
        region = region or self._region_of.get(server_id, "")
        try:
            rips = (await self._request("GET", f"/v1/reserved_fixed_ips/{p}/{region}")).get("results", [])
            ports = [r["port_id"] for r in rips
                     if ((r.get("reservation") or {}).get("resource_id") == server_id)]
        except ProviderError:
            ports = []
        params = {"delete_floatings": "true"}
        if ports:
            params["reserved_fixed_ips"] = ",".join(ports)
        try:
            await self._request("DELETE", f"/v1/instances/{p}/{region}/{server_id}", params=params)
        except ProviderError as e:
            if e.status != 404:
                raise

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=True)

    async def _wait(self, region: str, server_id: str, timeout: int, need_active: bool) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.ipv4 and (s.status == "ACTIVE" or not need_active):
                return s
            if s.status == "ERROR" or time.time() > deadline:
                s.status = s.status if s.status == "ERROR" else "TIMEOUT"
                return s
            await asyncio.sleep(3)
