"""Google Cloud (Compute Engine) — https://compute.googleapis.com/compute/v1, OAuth2 access token from a
service account key (JSON file): a JWT signed with the key (RS256) is exchanged at oauth2.googleapis.com/token.

Fast mode: a static external IP is reserved on its own (regions/{r}/addresses), its address is known at
once, and a VM is created with that IP as its external IP (accessConfigs[0].natIP) — the server's real
public IP. Wrong addresses are released right away. A "region" in the bot is a GCP region (europe-north1…);
the zone comes with the tariff (flavor id "zone/machine type"). A server id is "zone/name".

Every server gets the network tag vps-buyer, opened by a firewall rule that lets all traffic in. The root
password and SSH keys are set by the startup script (GCP images turn password logins off)."""
import asyncio
import base64
import json as jsonlib
import logging
import secrets
import time

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .base import (Flavor, Image, IpPool, ProviderError, RegionStatus, Server, merge_scripts, open_access_script,
                   ssh_key_script)
from .hostvds import make_password, password_script

log = logging.getLogger(__name__)
API_URL = "https://compute.googleapis.com/compute/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/compute"
TAG = "vps-buyer"
FIREWALL = "vps-buyer-allow-all"
RELEASE_TIMEOUT = 300
_PENDING: set[asyncio.Task] = set()

REGIONS = {
    "europe-north1": ("FI", "Хамина"), "europe-north2": ("SE", "Стокгольм"), "europe-central2": ("PL", "Варшава"),
    "europe-west6": ("CH", "Цюрих"), "europe-west9": ("FR", "Париж"), "europe-west3": ("DE", "Франкфурт"),
    "europe-west10": ("DE", "Берлин"), "europe-west8": ("IT", "Милан"), "europe-west12": ("IT", "Турин"),
    "europe-southwest1": ("ES", "Мадрид"), "europe-west4": ("NL", "Эмсхавен"), "europe-west1": ("BE", "Сен-Гислен"),
    "europe-west2": ("GB", "Лондон"), "me-west1": ("IL", "Тель-Авив"), "me-central1": ("QA", "Доха"),
    "me-central2": ("SA", "Даммам"), "us-central1": ("US", "Айова"), "us-east1": ("US", "Южная Каролина"),
    "us-east4": ("US", "Вирджиния"), "us-east5": ("US", "Огайо"), "us-south1": ("US", "Даллас"),
    "us-west1": ("US", "Орегон"), "us-west2": ("US", "Лос-Анджелес"), "us-west3": ("US", "Солт-Лейк-Сити"),
    "us-west4": ("US", "Лас-Вегас"), "northamerica-northeast1": ("CA", "Монреаль"),
    "northamerica-northeast2": ("CA", "Торонто"), "northamerica-south1": ("MX", "Керетаро"),
    "southamerica-east1": ("BR", "Сан-Паулу"), "southamerica-west1": ("CL", "Сантьяго"),
    "asia-east1": ("TW", "Тайвань"), "asia-east2": ("HK", "Гонконг"), "asia-northeast1": ("JP", "Токио"),
    "asia-northeast2": ("JP", "Осака"), "asia-northeast3": ("KR", "Сеул"), "asia-south1": ("IN", "Мумбаи"),
    "asia-south2": ("IN", "Дели"), "asia-southeast1": ("SG", "Сингапур"), "asia-southeast2": ("ID", "Джакарта"),
    "australia-southeast1": ("AU", "Сидней"), "australia-southeast2": ("AU", "Мельбурн"),
    "africa-south1": ("ZA", "Йоханнесбург"),
}
# Public images (family = always the latest build). x86 only: e2 / n2d machine types.
IMAGES = [("ubuntu-os-cloud/ubuntu-2204-lts", "Ubuntu 22.04 LTS"),
          ("ubuntu-os-cloud/ubuntu-2404-lts-amd64", "Ubuntu 24.04 LTS"),
          ("debian-cloud/debian-12", "Debian 12"), ("debian-cloud/debian-11", "Debian 11")]
# Small machine types worth offering (shared-core first); approximate on-demand price in Europe, $/hour.
TYPES = {"e2-micro": 0.0092, "e2-small": 0.0184, "e2-medium": 0.0368, "e2-standard-2": 0.0737,
         "e2-standard-4": 0.1474, "e2-highcpu-2": 0.0544, "e2-highmem-2": 0.0994, "n2d-standard-2": 0.0927}


async def wait_pending(timeout: float | None = None) -> None:
    if _PENDING:
        await asyncio.wait(set(_PENDING), timeout=timeout)


def parse_key(secret: str) -> dict:
    """The service account key file (JSON) as the user sent it."""
    try:
        key = jsonlib.loads(secret)
    except ValueError:
        raise ProviderError("Это не JSON-ключ. Отправьте файл ключа сервисного аккаунта (.json)") from None
    if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
        raise ProviderError("Нужен ключ сервисного аккаунта (type: service_account) — см. инструкцию")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(key: dict, now: int, token_url: str = TOKEN_URL) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id", "")}
    claims = {"iss": key["client_email"], "scope": SCOPE, "aud": token_url, "iat": now, "exp": now + 3600}
    signing_input = f"{_b64(jsonlib.dumps(header).encode())}.{_b64(jsonlib.dumps(claims).encode())}"
    private = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    signature = private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


def _name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"  # GCP names: lowercase letters, digits, dashes


class GCPClient:
    supports_ports = True     # "ports" here are static external IPs
    reserves_ips = True
    max_held_ports = 0
    reserve_parallel = 2

    def __init__(self, secret: str, api_url: str = API_URL, token_url: str = TOKEN_URL):
        self.key = parse_key(secret)
        self.project = self.key.get("project_id", "")
        self.api_url = api_url.rstrip("/")
        self.token_url = token_url
        self._token: tuple[str, float] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._zones: dict[str, list[str]] = {}
        self._ips: dict[str, str] = {}       # address name → IP
        self._firewall = False

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # ---------- auth / requests ----------
    async def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        assertion = make_jwt(self.key, int(time.time()), self.token_url)
        async with self._session.post(self.token_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}) as r:
            body = await r.json(content_type=None)
        if r.status != 200 or "access_token" not in (body or {}):
            raise ProviderError(f"Google: ключ не принят ({(body or {}).get('error_description') or r.status})", 401)
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        url = path if path.startswith("http") else f"{self.api_url}/projects/{self.project}/{path}"
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            async with self._session.request(method, url, json=json, params=params, headers=headers) as r:
                body = await r.json(content_type=None) if r.content_length != 0 else None
            if r.status < 400:
                return body
            err = (body or {}).get("error") or {}
            reasons = {e.get("reason", "") for e in err.get("errors", [])} | {err.get("status", "")}
            msg = err.get("message") or r.reason
            if r.status in (429, 503) and attempt < 3 and "quotaExceeded" not in reasons:
                await asyncio.sleep(5)
                continue
            raise self._error(f"Google {method} {path.split('?')[0]} → {r.status}: {msg}", r.status, reasons, msg)

    @staticmethod
    def _error(text: str, status: int, reasons: set, msg: str) -> ProviderError:
        low = msg.lower()
        if status in (401, 403) and ("has not been used" in low or "SERVICE_DISABLED" in reasons or "is disabled" in low):
            return ProviderError(text + " — включите Compute Engine API в проекте (и привяжите биллинг)", 403)
        if "quotaExceeded" in reasons or "QUOTA_EXCEEDED" in low or "quota" in low:
            return ProviderError(text, 429 if "instance" in low or "cpus" in low else 403)
        if "ZONE_RESOURCE_POOL_EXHAUSTED" in msg or "does not have enough resources" in low:
            return ProviderError(text, 429)
        if "resourceInUseByAnotherResource" in reasons or "being used by" in low:
            return ProviderError(text, 409)
        return ProviderError(text, status)

    async def _wait_op(self, op: dict, timeout: int = 180) -> dict:
        """Long-running operation (zone / region / global) until DONE; raises its error."""
        deadline = time.time() + timeout
        while op.get("status") != "DONE":
            if time.time() > deadline:
                raise ProviderError(f"Google: операция {op.get('operationType')} не завершилась", 429)
            await asyncio.sleep(2)
            op = await self._request("GET", op["selfLink"])
        if op.get("error"):
            errs = op["error"].get("errors", [])
            msg = "; ".join(e.get("message", e.get("code", "")) for e in errs) or "ошибка"
            reasons = {e.get("code", "") for e in errs}
            status = 429 if reasons & {"QUOTA_EXCEEDED", "ZONE_RESOURCE_POOL_EXHAUSTED",
                                       "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"} else 400
            if "QUOTA_EXCEEDED" in reasons and op.get("operationType") == "insert" and "/addresses/" in op.get("targetLink", ""):
                status = 403  # out of static IPs: IP hunting gives way to servers
            raise ProviderError(f"Google {op.get('operationType')}: {msg}", status)
        return op

    # ---------- account ----------
    async def check(self) -> str:
        await self._request("GET", f"{self.api_url}/projects/{self.project}")
        return f"{self.project}, сервисный аккаунт {self.key['client_email'].split('@')[0]}"

    # ---------- regions ----------
    async def region_statuses(self) -> list[RegionStatus]:
        data = await self._request("GET", "regions")
        result = []
        for r in sorted((data or {}).get("items", []), key=lambda r: r["name"]):
            self._zones[r["name"]] = sorted(z.rsplit("/", 1)[1] for z in r.get("zones", []))
            country, city = REGIONS.get(r["name"], ("", r["name"]))
            ok = r.get("status") == "UP"
            result.append(RegionStatus(r["name"], ok, "" if ok else "недоступен", None, [], country, city))
        return result

    async def _region_zones(self, region: str) -> list[str]:
        if region not in self._zones:
            r = await self._request("GET", f"regions/{region}")
            self._zones[region] = sorted(z.rsplit("/", 1)[1] for z in r.get("zones", []))
        return self._zones[region]

    async def ip_pools(self, region: str) -> list[IpPool]:
        return []  # no subnet list: blocks are learned from reserved IPs (scouting) and purchases

    # ---------- machine types / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        flavors, seen = [], set()
        for zone in await self._region_zones(region):
            data = await self._request("GET", f"zones/{zone}/machineTypes",
                                       params={"filter": "name = e2-* OR name = n2d-standard-2"})
            for t in (data or {}).get("items", []):
                name = t["name"]
                if name in seen or name not in TYPES:
                    continue
                seen.add(name)
                flavors.append(Flavor(f"{zone}/{name}", name, int(t.get("guestCpus") or 0), int(t.get("memoryMb") or 0),
                                      10, f"≈${TYPES[name] * 730:.0f}/мес · {zone}"))
        return sorted(flavors, key=lambda f: TYPES[f.name])

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        return flavors[0], TYPES[flavors[0].name] * 730

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        return [Image(i, name) for i, name in IMAGES]

    # ---------- static IPs (fast mode) ----------
    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None) -> tuple[str, str]:
        name = _name("vpsb-ip")
        op = await self._request("POST", f"regions/{region}/addresses",
                                 {"name": name, "addressType": "EXTERNAL", "networkTier": "PREMIUM"})
        await self._wait_op(op, 60)
        addr = await self._request("GET", f"regions/{region}/addresses/{name}")
        self._ips[name] = addr.get("address", "")
        return name, addr.get("address")

    async def delete_port(self, region: str, port_id: str) -> None:
        try:
            op = await self._request("DELETE", f"regions/{region}/addresses/{port_id}")
            await self._wait_op(op, 60)
        except ProviderError as e:
            if e.status != 404:
                raise

    # ---------- servers ----------
    async def _ensure_firewall(self) -> None:
        if self._firewall:
            return
        try:
            await self._request("GET", f"global/firewalls/{FIREWALL}")
        except ProviderError as e:
            if e.status != 404:
                raise
            op = await self._request("POST", "global/firewalls", {
                "name": FIREWALL, "network": "global/networks/default", "direction": "INGRESS", "priority": 900,
                "sourceRanges": ["0.0.0.0/0"], "allowed": [{"IPProtocol": "all"}], "targetTags": [TAG],
                "description": "VPS Buyer: all ports open on its servers"})
            await self._wait_op(op, 60)
        self._firewall = True

    @staticmethod
    def _server(i: dict) -> Server:
        zone = i.get("zone", "").rsplit("/", 1)[-1]
        nat = [ac.get("natIP") for ni in i.get("networkInterfaces", []) for ac in ni.get("accessConfigs", [])
               if ac.get("natIP")]
        status = {"RUNNING": "ACTIVE", "TERMINATED": "ERROR", "STOPPED": "ERROR"}.get(i.get("status"), "BUILD")
        return Server(f"{zone}/{i['name']}", i["name"], status, nat, region=zone.rsplit("-", 1)[0])

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        await self._ensure_firewall()
        zone, mtype = flavor.id.split("/", 1)
        password = None if ssh_key else make_password()
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        script = merge_scripts(password_script(password) if password else None,
                               ssh_key_script(keys) if keys else None)
        if not password:  # own key: still open every port
            script = merge_scripts(script, open_access_script(False))
        project, family = image_id.split("/", 1)
        access = {"name": "External NAT", "type": "ONE_TO_ONE_NAT", "networkTier": "PREMIUM"}
        if port_id:
            access["natIP"] = self._ips.get(port_id) or (await self._request(
                "GET", f"regions/{region}/addresses/{port_id}")).get("address")
        body = {
            "name": name, "machineType": f"zones/{zone}/machineTypes/{mtype}",
            "disks": [{"boot": True, "autoDelete": True, "initializeParams": {
                "sourceImage": f"projects/{project}/global/images/family/{family}",
                "diskSizeGb": str(max(10, boot_volume_gb))}}],
            "networkInterfaces": [{"network": "global/networks/default", "accessConfigs": [access]}],
            "metadata": {"items": [{"key": "startup-script", "value": script}]},
            "tags": {"items": [TAG]}, "labels": {"vps-buyer": "1"},
        }
        op = await self._request("POST", f"zones/{zone}/instances", body)
        await self._wait_op(op, 180)
        return Server(f"{zone}/{name}", name, "BUILD", [], password, region=region)

    async def get_server(self, region: str, server_id: str) -> Server:
        zone, name = server_id.split("/", 1)
        return self._server(await self._request("GET", f"zones/{zone}/instances/{name}"))

    async def list_servers(self) -> list[Server]:
        data = await self._request("GET", "aggregated/instances")
        result = []
        for scope in ((data or {}).get("items") or {}).values():
            result += [self._server(i) for i in scope.get("instances", [])]
        return result

    async def delete_server(self, region: str, server_id: str) -> None:
        zone, name = server_id.split("/", 1)
        region = region or zone.rsplit("-", 1)[0]
        nat = []
        try:
            srv = await self.get_server(region, server_id)
            nat = srv.ipv4
        except ProviderError as e:
            if e.status != 404:
                raise
            return
        await self._request("DELETE", f"zones/{zone}/instances/{name}")
        # a static IP outlives its VM (and is billed while idle): release ours once the VM is gone
        task = asyncio.ensure_future(self._release_static(region, nat))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _release_static(self, region: str, ips: list[str]) -> None:
        if not ips:
            return
        async with GCPClient(jsonlib.dumps(self.key), self.api_url, self.token_url) as c:
            try:
                data = await c._request("GET", f"regions/{region}/addresses")
            except Exception:
                log.exception("GCP: listing addresses failed")
                return
            names = [a["name"] for a in (data or {}).get("items", [])
                     if a.get("address") in ips and a["name"].startswith("vpsb-ip-")]
            deadline = time.time() + RELEASE_TIMEOUT
            while names:
                for n in list(names):
                    try:
                        await c.delete_port(region, n)
                        names.remove(n)
                    except Exception as e:
                        if time.time() > deadline:
                            log.error("GCP static IP %s was not released: %s", n, e)
                            names.remove(n)
                if names:
                    await asyncio.sleep(10)

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
