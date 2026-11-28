"""UpCloud — https://api.upcloud.com/1.3. Auth: an API token (Authorization: Bearer) or an API user
"username:password" (HTTP Basic). Hourly billing from the account's credit.

No way to take a primary IP before a server (floating IPs are extra addresses), so the bot buys servers:
create → look at the public IPv4 → stop + delete if it is not in the chosen subnets. UpCloud only deletes
stopped servers, so a delete stops the server first and finishes in the background."""
import asyncio
import base64
import logging
import time

import aiohttp

from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server, merge_scripts, open_access_script, \
    ssh_key_script
from .hostvds import make_password, password_script

log = logging.getLogger(__name__)
API_URL = "https://api.upcloud.com/1.3"
DELETE_TIMEOUT = 600
_PENDING: set[asyncio.Task] = set()

ZONES = {
    "fi-hel1": ("FI", "Хельсинки #1"), "fi-hel2": ("FI", "Хельсинки #2"), "se-sto1": ("SE", "Стокгольм"),
    "de-fra1": ("DE", "Франкфурт"), "pl-waw1": ("PL", "Варшава"), "nl-ams1": ("NL", "Амстердам"),
    "uk-lon1": ("GB", "Лондон"), "es-mad1": ("ES", "Мадрид"), "dk-cph1": ("DK", "Копенгаген"),
    "no-svg1": ("NO", "Ставангер"), "us-chi1": ("US", "Чикаго"), "us-nyc1": ("US", "Нью-Йорк"),
    "us-sjo1": ("US", "Сан-Хосе"), "sg-sin1": ("SG", "Сингапур"), "au-syd1": ("AU", "Сидней"),
}
TEMPLATES = ("Ubuntu Server 22.04", "Ubuntu Server 24.04", "Debian GNU/Linux 12", "Debian GNU/Linux 11",
             "AlmaLinux 9", "Rocky Linux 9")


async def wait_pending(timeout: float | None = None) -> None:
    if _PENDING:
        await asyncio.wait(set(_PENDING), timeout=timeout)


def _auth(secret: str) -> str:
    secret = secret.strip()
    if ":" in secret and " " not in secret and not secret.startswith("ucat_"):
        return "Basic " + base64.b64encode(secret.encode()).decode()  # API user "username:password"
    return f"Bearer {secret}"


class UpCloudClient:
    def __init__(self, secret: str, api_url: str = API_URL):
        self.secret = secret
        self.api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._zone_of: dict[str, str] = {}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        headers = {"Authorization": _auth(self.secret), "Accept": "application/json"}
        for attempt in range(4):
            async with self._session.request(method, self.api_url + path, json=json, params=params,
                                             headers=headers) as r:
                if r.status == 429 and attempt < 3:
                    await asyncio.sleep(5)
                    continue
                if r.status == 204:
                    return None
                body = await r.json(content_type=None) if r.content_length != 0 else None
                if r.status >= 400:
                    err = (body or {}).get("error") or {}
                    code, msg = err.get("error_code", ""), err.get("error_message") or r.reason
                    status = r.status
                    if r.status == 401:
                        msg = f"неверный токен или логин/пароль API ({msg})"
                    elif code in ("SERVER_CREATING_LIMIT_REACHED", "CORES_LIMIT_REACHED", "MEMORY_LIMIT_REACHED",
                                  "SERVER_LIMIT_REACHED", "IP_ADDRESS_LIMIT_REACHED", "STORAGE_LIMIT_REACHED") \
                            or "LIMIT" in code:
                        status = 429  # account limit: our deletions free it up
                    elif code in ("INSUFFICIENT_CREDITS", "TRIAL_PLAN", "PAYMENT_REQUIRED"):
                        msg = f"недостаточно средств на балансе UpCloud ({msg})"
                    raise ProviderError(f"UpCloud {method} {path} → {r.status} {code}: {msg}", status)
                return body
        raise ProviderError(f"UpCloud {method} {path}: слишком много запросов", 429)

    # ---------- account ----------
    async def check(self) -> str:
        acc = (await self._request("GET", "/account"))["account"]
        credits = float(acc.get("credits") or 0) / 100  # cents
        return f"{acc.get('username', 'UpCloud')}, баланс ${credits:.2f}"

    # ---------- zones ----------
    async def region_statuses(self) -> list[RegionStatus]:
        zones = (await self._request("GET", "/zone"))["zones"]["zone"]
        result = []
        for z in sorted(zones, key=lambda z: z["id"]):
            if z.get("public", "yes") != "yes":
                continue
            country, city = ZONES.get(z["id"], ("", z.get("description") or z["id"]))
            result.append(RegionStatus(z["id"], True, "", None, [], country, city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        return []  # no subnet list: blocks are learned from purchases and scouting

    # ---------- plans / templates ----------
    async def _prices(self, zone: str) -> dict[str, float]:
        """Hourly price in $ per plan in a zone (the API gives cents)."""
        try:
            data = (await self._request("GET", "/price"))["prices"]["zone"]
        except ProviderError:
            return {}
        z = next((x for x in data if x.get("name") == zone), {})
        return {k.removeprefix("server_plan_"): float(v.get("price") or 0) / 100
                for k, v in z.items() if k.startswith("server_plan_") and isinstance(v, dict)}

    async def list_flavors(self, region: str) -> list[Flavor]:
        plans, prices = await asyncio.gather(self._request("GET", "/plan"), self._prices(region))
        flavors = []
        for p in plans["plans"]["plan"]:
            name = p["name"]
            if name.startswith(("GPU", "HICPU", "HIMEM")):
                continue
            hour = prices.get(name)
            if prices and hour is None:  # not sold in this zone
                continue
            flavors.append(Flavor(name, name, int(p.get("core_number") or 0), int(p.get("memory_amount") or 0),
                                  int(p.get("storage_size") or 0),
                                  f"${hour * 730:.0f}/мес · ${hour:.4f}/час" if hour else ""))
        flavors.sort(key=lambda f: (prices.get(f.id, 99), f.vcpus, f.ram_mb))
        return flavors[:20]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        prices = await self._prices(region)
        return flavors[0], prices.get(flavors[0].id, 0.01)

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        data = await self._request("GET", "/storage/template")
        images = [Image(s["uuid"], s.get("title", "")) for s in data["storages"]["storage"]
                  if "windows" not in s.get("title", "").lower()]
        images.sort(key=lambda i: (not i.name.startswith(TEMPLATES), i.name))
        return images

    # ---------- servers ----------
    def _server(self, s: dict) -> Server:
        ips = s.get("ip_addresses", {}).get("ip_address", []) if isinstance(s.get("ip_addresses"), dict) else []
        public = [a["address"] for a in ips if a.get("access") == "public" and a.get("family") == "IPv4"
                  and a.get("address") and a.get("floating") != "yes"]
        state = s.get("state", "")
        status = {"started": "ACTIVE", "error": "ERROR", "stopped": "ERROR"}.get(state, "BUILD")
        if s.get("zone"):
            self._zone_of[s["uuid"]] = s["zone"]
        return Server(s["uuid"], s.get("title") or s.get("hostname", ""), status, public, s.get("password"),
                      region=s.get("zone", ""))

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        password = None if ssh_key else make_password()
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        script = merge_scripts(password_script(password) if password else open_access_script(False),
                               ssh_key_script(keys) if keys else None)
        body = {"server": {
            "zone": region, "title": name, "hostname": name, "plan": flavor.id, "metadata": "yes",
            "user_data": script,
            "login_user": {"username": "root", "create_password": "no",
                           **({"ssh_keys": {"ssh_key": keys}} if keys else {})},
            "storage_devices": {"storage_device": [{
                "action": "clone", "storage": image_id, "title": f"{name}-disk",
                "size": max(flavor.disk_gb or 10, 10), "tier": "maxiops"}]},
            "networking": {"interfaces": {"interface": [
                {"type": "public", "ip_addresses": {"ip_address": [{"family": "IPv4"}]}},
                {"type": "utility", "ip_addresses": {"ip_address": [{"family": "IPv4"}]}}]}},
        }}
        srv = self._server((await self._request("POST", "/server", body))["server"])
        srv.admin_pass = password
        return srv

    async def get_server(self, region: str, server_id: str) -> Server:
        return self._server((await self._request("GET", f"/server/{server_id}"))["server"])

    async def list_servers(self) -> list[Server]:
        data = await self._request("GET", "/server")
        result = []
        for s in (data or {}).get("servers", {}).get("server", []):
            srv = self._server(s)
            if not srv.ipv4 and s.get("state") == "started":
                try:
                    srv = await self.get_server(s.get("zone", ""), s["uuid"])
                except ProviderError:
                    pass
            result.append(srv)
        return result

    async def delete_server(self, region: str, server_id: str) -> None:
        """Stop (hard) and delete with its disk. UpCloud deletes only stopped servers and a fresh server is
        busy while it installs, so this finishes in the background."""
        try:
            await self._request("GET", f"/server/{server_id}")
        except ProviderError as e:
            if e.status == 404:
                return
            raise
        task = asyncio.ensure_future(self._stop_and_delete(server_id))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _stop_and_delete(self, server_id: str) -> None:
        async with UpCloudClient(self.secret, self.api_url) as c:
            deadline = time.time() + DELETE_TIMEOUT
            while True:
                try:
                    s = (await c._request("GET", f"/server/{server_id}"))["server"]
                    if s.get("state") == "stopped":
                        await c._request("DELETE", f"/server/{server_id}", params={"storages": "1",
                                                                                   "backups": "delete"})
                        return
                    if s.get("state") == "started":
                        await c._request("POST", f"/server/{server_id}/stop",
                                         {"stop_server": {"stop_type": "hard", "timeout": "60"}})
                except ProviderError as e:
                    if e.status == 404:
                        return
                    log.info("UpCloud delete %s: %s (retrying)", server_id, e)
                except Exception:
                    log.exception("UpCloud delete %s", server_id)
                if time.time() > deadline:
                    log.error("UpCloud server %s was not deleted", server_id)
                    return
                await asyncio.sleep(5)

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
            if s.status == "ERROR" or time.time() > deadline:
                s.status = s.status if s.status == "ERROR" else "TIMEOUT"
                return s
            await asyncio.sleep(3)
