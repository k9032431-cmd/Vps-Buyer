"""Civo — https://api.civo.com/v2, "Authorization: bearer <API key>" (https://www.civo.com/api).

Civo publishes no subnet list: IP blocks are learned from your servers and purchases ("Any IP" works too).
Most calls take the region; an instance needs the region's default network and a disk image
(template_id). The root password comes back as initial_password when no SSH key id is given."""
import asyncio
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, prefix24, ssh_key_script

API_URL = "https://api.civo.com/v2"
CITIES = {"LON1": ("GB", "Лондон"), "NYC1": ("US", "Нью-Йорк"), "FRA1": ("DE", "Франкфурт"),
          "PHX1": ("US", "Финикс"), "MUM1": ("IN", "Мумбаи")}


class CivoClient:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._keys: dict[str, str] = {}
        self._regions_of: dict[str, str] = {}   # instance id → region (GET/DELETE need it)

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        headers = {"Authorization": f"bearer {self.token}", "Accept": "application/json"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, params=params,
                                             headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(10)
                    continue
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status >= 400:
                    msg = (body or {}).get("reason") or (body or {}).get("code") if isinstance(body, dict) else None
                    if r.status in (401, 403):
                        msg = f"неверный API-ключ ({msg})"
                    raise ProviderError(f"Civo {method} {path} → {r.status}: {msg or r.reason}", r.status)
                return body

    async def check(self) -> str:
        q = await self._request("GET", "/quota")
        return f"квота инстансов: {q.get('instance_count_usage', 0)}/{q.get('instance_count_limit', '?')}"

    async def _instances(self, region: str) -> list[dict]:
        data = await self._request("GET", "/instances", params={"region": region, "per_page": 200})
        return data.get("items", []) if isinstance(data, dict) else []

    async def region_statuses(self) -> list[RegionStatus]:
        result = []
        for r in await self._request("GET", "/regions"):
            if not (r.get("features") or {}).get("iaas", True):
                continue
            ok = not r.get("out_of_capacity")
            code = r["code"]
            country, city = CITIES.get(code.upper(), ((r.get("country") or "").upper(), r.get("name", code)))
            try:
                ips = [i.get("public_ip") for i in await self._instances(code) if i.get("public_ip")]
            except ProviderError:
                ips = []
            pools: dict[str, IpPool] = {}
            for ip in ips:
                pools.setdefault(prefix24(ip), IpPool(prefix24(ip), None, [])).cidrs.append(ip)
            result.append(RegionStatus(code, ok, "" if ok else "нет мощностей", ips[0] if ips else None,
                                       list(pools.values()), country, city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        pools: dict[str, IpPool] = {}
        for i in await self._instances(region):
            ip = i.get("public_ip")
            if ip:
                pools.setdefault(prefix24(ip), IpPool(prefix24(ip), None, [])).cidrs.append(ip)
        return list(pools.values())

    async def list_flavors(self, region: str) -> list[Flavor]:
        sizes = [s for s in await self._request("GET", "/sizes")
                 if s.get("selectable", True) and not any(w in s["name"] for w in ("kube", "db", "gpu", "k3s"))]
        sizes.sort(key=lambda s: (s.get("cpu_cores", 0), s.get("ram_mb", 0)))
        return [Flavor(s["name"], s.get("nice_name") or s["name"], int(s.get("cpu_cores") or 0),
                       int(s.get("ram_mb") or 0), int(s.get("disk_gb") or 0)) for s in sizes][:30]

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        images = await self._request("GET", "/disk_images", params={"region": region})
        return [Image(i["id"], f"{i.get('distribution') or i.get('name', '')} {i.get('version', '')}".strip())
                for i in images if i.get("state", "available") == "available"]

    async def _key_id(self, public_key: str) -> str | None:
        if public_key in self._keys:
            return self._keys[public_key]
        body = public_key.split()[1]
        try:
            for k in await self._request("GET", "/sshkeys"):
                if body in str(k.get("public_key", "")):
                    self._keys[public_key] = k["id"]
                    return k["id"]
            parts = public_key.split()
            created = await self._request("POST", "/sshkeys", {
                "name": f"vps-buyer-{parts[2] if len(parts) > 2 else body[-8:]}", "public_key": public_key})
            self._keys[public_key] = created["id"]
            return created["id"]
        except ProviderError:
            return None

    def _server(self, i: dict) -> Server:
        ip = i.get("public_ip")
        return Server(str(i["id"]), i.get("hostname", ""), "ACTIVE" if i.get("status") == "ACTIVE" else "BUILD",
                      [ip] if ip else [], i.get("initial_password") or None)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        networks = await self._request("GET", "/networks", params={"region": region})
        net = next((n for n in networks if n.get("default")), networks[0] if networks else None)
        if not net:
            raise ProviderError("Civo: в регионе нет сети по умолчанию")
        body = {"hostname": name, "size": flavor.id, "region": region, "network_id": net["id"],
                "template_id": image_id, "public_ip": "create", "initial_user": "root", "count": 1}
        key_id = await self._key_id(ssh_key) if ssh_key else None
        if key_id:
            body["ssh_key_id"] = key_id
        rest = ([] if key_id else ([ssh_key] if ssh_key else [])) + list(extra_keys or [])
        if rest:  # Civo takes one key id; any other key goes in by the first-boot script
            body["script"] = ssh_key_script(rest)
        srv = self._server(await self._request("POST", "/instances", body))
        self._regions_of[srv.id] = region
        if ssh_key:
            srv.admin_pass = None
        return srv

    async def get_server(self, region: str, server_id: str) -> Server:
        region = region or self._regions_of.get(server_id, "")
        return self._server(await self._request("GET", f"/instances/{server_id}", params={"region": region}))

    async def delete_server(self, region: str, server_id: str) -> None:
        region = region or self._regions_of.get(server_id, "")
        try:
            await self._request("DELETE", f"/instances/{server_id}", params={"region": region})
        except ProviderError as e:
            if e.status != 404:
                raise

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        s = await self._wait(region, server_id, timeout, need_active=True)
        return s

    async def _wait(self, region: str, server_id: str, timeout: int, need_active: bool) -> Server:
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.ipv4 and (s.status == "ACTIVE" or not need_active):
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(3)
