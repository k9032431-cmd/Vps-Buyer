"""Tencent Cloud (CVM + EIP) — API 3.0: POST https://{cvm|vpc}.tencentcloudapi.com, JSON body, headers
X-TC-Action / X-TC-Version / X-TC-Timestamp / X-TC-Region, signed with TC3-HMAC-SHA256 (SecretId + SecretKey).

Fast mode: an EIP is allocated on its own (vpc AllocateAddresses), its address is known at once; a server
is created WITHOUT a public IP and the EIP is bound to it (vpc AssociateAddress) — the EIP is then the
server's public IP for everything. Wrong EIPs are released right away. A "region" in the bot is a
Tencent region (eu-frankfurt…); the zone comes with the tariff (flavor id "zone/instance type").

Every server gets a security group that lets all traffic in (vps-buyer-allow-all)."""
import asyncio
import base64
import hashlib
import hmac
import json as jsonlib
import logging
import secrets
import string
import time
from datetime import datetime, timezone

import aiohttp

from ..config import config
from .base import (Flavor, Image, IpPool, ProviderError, RegionStatus, Server, merge_scripts, open_access_script,
                   ssh_key_script)

log = logging.getLogger(__name__)
VERSION = "2017-03-12"   # cvm and vpc
SECURITY_GROUP = "vps-buyer-allow-all"
RELEASE_TIMEOUT = 300
_PENDING: set[asyncio.Task] = set()

# region → (country, city in Russian)
REGIONS = {
    "ap-guangzhou": ("CN", "Гуанчжоу"), "ap-shanghai": ("CN", "Шанхай"), "ap-nanjing": ("CN", "Нанкин"),
    "ap-beijing": ("CN", "Пекин"), "ap-chengdu": ("CN", "Чэнду"), "ap-chongqing": ("CN", "Чунцин"),
    "ap-hongkong": ("HK", "Гонконг"), "ap-singapore": ("SG", "Сингапур"), "ap-bangkok": ("TH", "Бангкок"),
    "ap-jakarta": ("ID", "Джакарта"), "ap-seoul": ("KR", "Сеул"), "ap-tokyo": ("JP", "Токио"),
    "ap-mumbai": ("IN", "Мумбаи"), "na-siliconvalley": ("US", "Силиконовая долина"),
    "na-ashburn": ("US", "Эшберн"), "na-toronto": ("CA", "Торонто"), "eu-frankfurt": ("DE", "Франкфурт"),
    "eu-moscow": ("RU", "Москва"), "sa-saopaulo": ("BR", "Сан-Паулу"), "ap-taipei": ("TW", "Тайбэй"),
    "me-saudi-arabia": ("SA", "Эр-Рияд"),
}


async def wait_pending(timeout: float | None = None) -> None:
    if _PENDING:
        await asyncio.wait(set(_PENDING), timeout=timeout)


def make_password(length: int = 16) -> str:
    """Linux CVM: 8–30 chars with lower, upper, digits and one of Tencent's allowed symbols."""
    while True:
        pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length - 2))
        pw += secrets.choice("!@#%^*-+=") + secrets.choice(string.digits)
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw):
            return pw


def split_secret(secret: str) -> tuple[str, str]:
    """"SecretId SecretKey" (space, new line, colon or comma between them)."""
    parts = [p for p in secret.replace(":", " ").replace(",", " ").split() if p]
    if len(parts) != 2:
        raise ProviderError("Отправьте SecretId и SecretKey одним сообщением через пробел")
    return parts[0], parts[1]


def sign(secret_id: str, secret_key: str, service: str, host: str, payload: str, timestamp: int) -> str:
    """Authorization header value, TC3-HMAC-SHA256."""
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    ct = "application/json; charset=utf-8"
    canonical = "\n".join(["POST", "/", "", f"content-type:{ct}\nhost:{host}\n", "content-type;host",
                           hashlib.sha256(payload.encode()).hexdigest()])
    scope = f"{date}/{service}/tc3_request"
    to_sign = "\n".join(["TC3-HMAC-SHA256", str(timestamp), scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    key = h(h(h(("TC3" + secret_key).encode(), date), service), "tc3_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders=content-type;host, "
            f"Signature={signature}")


# errors that mean "try again a bit later" / "account or stock limit" rather than "this request is wrong"
BUSY_CODES = ("RequestLimitExceeded", "ResourceInsufficient", "ResourceUnavailable", "LimitExceeded",
              "AddressQuotaLimitExceeded", "InstancesQuotaLimitExceeded", "ResourcesSoldOut")


class TencentClient:
    supports_ports = True     # "ports" here are EIPs: allocate an IP first, then build on it
    reserves_ips = True
    max_held_ports = 0
    reserve_parallel = 2

    def __init__(self, secret: str, host: str | None = None, base_url: str | None = None):
        self.secret_id, self.secret_key = split_secret(secret)
        self.host = host or config.tencent_api_host
        self.base_url = base_url  # tests: http://127.0.0.1:port → /cvm, /vpc
        self._session: aiohttp.ClientSession | None = None
        self._sg: dict[str, str] = {}
        self._bind: dict[str, str] = {}        # instance id → EIP id waiting to be bound
        self._eip_ip: dict[str, str] = {}      # EIP id → address
        self._region_of: dict[str, str] = {}   # instance id → region

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def call(self, service: str, action: str, region: str = "", params: dict | None = None) -> dict:
        host = f"{service}.{self.host}"
        payload = jsonlib.dumps(params or {}, separators=(",", ":"))
        for attempt in range(4):
            ts = int(time.time())
            headers = {"Authorization": sign(self.secret_id, self.secret_key, service, host, payload, ts),
                       "Content-Type": "application/json; charset=utf-8", "Host": host,
                       "X-TC-Action": action, "X-TC-Version": VERSION, "X-TC-Timestamp": str(ts),
                       "X-TC-Language": "en-US", **({"X-TC-Region": region} if region else {})}
            url = f"{self.base_url}/{service}" if self.base_url else f"https://{host}/"
            async with self._session.post(url, data=payload, headers=headers) as r:
                body = await r.json(content_type=None)
            resp = (body or {}).get("Response") or {}
            err = resp.get("Error")
            if not err:
                return resp
            code, msg = err.get("Code", ""), err.get("Message", "")
            if code == "RequestLimitExceeded" and attempt < 3:
                await asyncio.sleep(3)
                continue
            if code.startswith("AuthFailure"):
                status, msg = 401, f"неверный SecretId/SecretKey ({msg})"
            elif code.startswith("ResourceNotFound") or code.startswith("InvalidInstanceId.NotFound") \
                    or code in ("InvalidAddressId.NotFound", "InvalidParameterValue.AddressIdNotFound"):
                status = 404
            elif any(code.startswith(c) or c in code for c in BUSY_CODES):
                status = 429
            else:
                status = 400
            raise ProviderError(f"Tencent {action} → {code}: {msg}", status)
        raise ProviderError(f"Tencent {action}: слишком много запросов", 429)

    # ---------- account ----------
    async def check(self) -> str:
        regions = await self.call("cvm", "DescribeRegions", "ap-singapore")
        n = len([r for r in regions.get("RegionSet", []) if r.get("RegionState") == "AVAILABLE"])
        return f"{self.secret_id[:10]}…, регионов: {n}"

    # ---------- regions ----------
    async def region_statuses(self) -> list[RegionStatus]:
        regions = (await self.call("cvm", "DescribeRegions", "ap-singapore")).get("RegionSet", [])
        result = []
        for r in sorted(regions, key=lambda r: r["Region"]):
            rid = r["Region"]
            country, city = REGIONS.get(rid, ("", r.get("RegionName") or rid))
            ok = r.get("RegionState") == "AVAILABLE"
            result.append(RegionStatus(rid, ok, "" if ok else "недоступен", None, [], country, city))
        return result

    async def ip_pools(self, region: str) -> list[IpPool]:
        return []  # no subnet list: blocks are learned from EIPs (scouting) and purchases

    # ---------- tariffs / images ----------
    async def list_flavors(self, region: str) -> list[Flavor]:
        data = await self.call("cvm", "DescribeZoneInstanceConfigInfos", region, {
            "Filters": [{"Name": "instance-charge-type", "Values": ["POSTPAID_BY_HOUR"]}]})
        best: dict[str, tuple[float, dict]] = {}
        for q in data.get("InstanceTypeQuotaSet", []):
            if q.get("Status") != "SELL":
                continue
            price = q.get("Price") or {}
            hour = price.get("UnitPriceDiscount") or price.get("UnitPrice")
            if hour is None:
                continue
            hour = float(hour)
            if q["InstanceType"] not in best or hour < best[q["InstanceType"]][0]:
                best[q["InstanceType"]] = (hour, q)
        flavors = sorted(best.values(), key=lambda x: x[0])
        return [Flavor(f"{q['Zone']}/{q['InstanceType']}", q["InstanceType"], int(q.get("Cpu") or 0),
                       int(float(q.get("Memory") or 0) * 1024), 20, f"${hour:.4f}/час · {q['Zone']}")
                for hour, q in flavors][:30]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        return flavors[0], float(flavors[0].price.split("$")[1].split("/")[0]) * 730

    async def list_images(self, region: str, flavor: Flavor | None = None) -> list[Image]:
        params = {"Filters": [{"Name": "image-type", "Values": ["PUBLIC_IMAGE"]}], "Limit": 100}
        if flavor:
            params["InstanceType"] = flavor.id.split("/", 1)[1]
        images = (await self.call("cvm", "DescribeImages", region, params)).get("ImageSet", [])
        return [Image(i["ImageId"], i.get("OsName") or i.get("ImageName") or i["ImageId"]) for i in images
                if (i.get("Platform") or "").lower() not in ("windows",)
                and "windows" not in (i.get("OsName") or "").lower()]

    # ---------- EIPs (fast mode) ----------
    async def _eips(self, region: str, ids: list[str] | None = None, filters: list | None = None) -> list[dict]:
        params: dict = {"Limit": 100}
        if ids:
            params["AddressIds"] = ids
        if filters:
            params["Filters"] = filters
        return (await self.call("vpc", "DescribeAddresses", region, params)).get("AddressSet", [])

    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None) -> tuple[str, str]:
        data = await self.call("vpc", "AllocateAddresses", region, {"AddressCount": 1})
        eid = (data.get("AddressSet") or [None])[0]
        if not eid:
            raise ProviderError("Tencent AllocateAddresses: EIP не выдан")
        ip = None
        for _ in range(10):  # the address is filled in within a second or two
            eips = await self._eips(region, [eid])
            ip = eips[0].get("AddressIp") if eips else None
            if ip:
                break
            await asyncio.sleep(1)
        self._eip_ip[eid] = ip or ""
        return eid, ip

    async def delete_port(self, region: str, port_id: str) -> None:
        try:
            await self.call("vpc", "ReleaseAddresses", region, {"AddressIds": [port_id]})
        except ProviderError as e:
            if e.status != 404:
                raise

    # ---------- servers ----------
    async def _security_group(self, region: str) -> str:
        if region in self._sg:
            return self._sg[region]
        found = (await self.call("vpc", "DescribeSecurityGroups", region, {
            "Filters": [{"Name": "security-group-name", "Values": [SECURITY_GROUP]}]})).get("SecurityGroupSet", [])
        if found:
            sg = found[0]["SecurityGroupId"]
        else:
            sg = (await self.call("vpc", "CreateSecurityGroup", region, {
                "GroupName": SECURITY_GROUP, "GroupDescription": "VPS Buyer: all traffic allowed"}))["SecurityGroup"]["SecurityGroupId"]
            rule = {"Protocol": "ALL", "CidrBlock": "0.0.0.0/0", "Action": "ACCEPT"}
            await self.call("vpc", "CreateSecurityGroupPolicies", region, {
                "SecurityGroupId": sg, "SecurityGroupPolicySet": {"Ingress": [rule], "Egress": [rule]}})
        self._sg[region] = sg
        return sg

    @staticmethod
    def _server(i: dict) -> Server:
        state = i.get("InstanceState", "")
        status = {"RUNNING": "ACTIVE", "LAUNCH_FAILED": "ERROR", "TERMINATING": "DELETED",
                  "SHUTDOWN": "ERROR"}.get(state, "BUILD")
        return Server(i["InstanceId"], i.get("InstanceName", ""), status, list(i.get("PublicIpAddresses") or []),
                      region=(i.get("Placement") or {}).get("Zone", "").rsplit("-", 1)[0])

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        zone, itype = flavor.id.split("/", 1)
        password = make_password()
        keys = ([ssh_key] if ssh_key else []) + list(extra_keys or [])
        script = merge_scripts(open_access_script(not ssh_key), ssh_key_script(keys) if keys else None)
        params = {
            "InstanceChargeType": "POSTPAID_BY_HOUR", "Placement": {"Zone": zone}, "InstanceType": itype,
            "ImageId": image_id, "InstanceName": name, "InstanceCount": 1,
            "SystemDisk": {"DiskType": "CLOUD_PREMIUM", "DiskSize": max(20, boot_volume_gb)},
            "InternetAccessible": {"InternetChargeType": "TRAFFIC_POSTPAID_BY_HOUR", "InternetMaxBandwidthOut": 10,
                                   "PublicIpAssigned": not port_id},
            "LoginSettings": {"Password": password},
            "SecurityGroupIds": [await self._security_group(region)],
            "UserData": base64.b64encode(script.encode()).decode(),
        }
        try:
            data = await self.call("cvm", "RunInstances", region, params)
        except ProviderError as e:
            if "DiskType" not in str(e) and "CLOUD_PREMIUM" not in str(e):
                raise
            params["SystemDisk"]["DiskType"] = "CLOUD_BSSD"  # newer zones sell only the new disk types
            data = await self.call("cvm", "RunInstances", region, params)
        iid = data["InstanceIdSet"][0]
        self._region_of[iid] = region
        if port_id:
            self._bind[iid] = port_id  # bound as soon as the server runs (Tencent needs it RUNNING)
        return Server(iid, name, "BUILD", [], None if ssh_key else password, region=region)

    async def get_server(self, region: str, server_id: str) -> Server:
        region = region or self._region_of.get(server_id, "")
        found = (await self.call("cvm", "DescribeInstances", region, {"InstanceIds": [server_id]})).get("InstanceSet", [])
        if not found:
            raise ProviderError(f"Tencent: сервер {server_id} не найден", 404)
        return self._server(found[0])

    async def list_servers(self) -> list[Server]:
        result = []
        regions = [r for r in (await self.call("cvm", "DescribeRegions", "ap-singapore")).get("RegionSet", [])
                   if r.get("RegionState") == "AVAILABLE"]

        async def one(rid: str):
            try:
                data = await self.call("cvm", "DescribeInstances", rid, {"Limit": 100})
            except ProviderError:
                return []
            servers = [self._server(i) for i in data.get("InstanceSet", [])]
            for s in servers:
                s.region = rid
                self._region_of[s.id] = rid
            return servers
        for chunk in await asyncio.gather(*(one(r["Region"]) for r in regions)):
            result += chunk
        return result

    async def delete_server(self, region: str, server_id: str) -> None:
        region = region or self._region_of.get(server_id, "")
        try:
            eips = [a["AddressId"] for a in await self._eips(region, filters=[{"Name": "instance-id",
                                                                               "Values": [server_id]}])]
        except ProviderError:
            eips = []
        pending = self._bind.pop(server_id, None)
        if pending and pending not in eips:
            eips.append(pending)
        try:
            await self.call("cvm", "TerminateInstances", region, {"InstanceIds": [server_id]})
        except ProviderError as e:
            if e.status != 404:
                raise
        if eips:  # an EIP outlives its server (and is billed while idle) — release it once it is unbound
            task = asyncio.ensure_future(self._release_later(region, eips))
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)

    async def _release_later(self, region: str, eips: list[str]) -> None:
        async with TencentClient(f"{self.secret_id} {self.secret_key}", self.host, self.base_url) as c:
            deadline = time.time() + RELEASE_TIMEOUT
            left = list(eips)
            while left:
                for eid in list(left):
                    try:
                        await c.delete_port(region, eid)
                        left.remove(eid)
                    except Exception as e:
                        if time.time() > deadline:
                            log.error("Tencent EIP %s was not released: %s", eid, e)
                            left.remove(eid)
                if left:
                    await asyncio.sleep(10)

    async def _bind_eip(self, region: str, server_id: str) -> None:
        eid = self._bind.get(server_id)
        if not eid:
            return
        try:
            await self.call("vpc", "AssociateAddress", region, {"AddressId": eid, "InstanceId": server_id})
            self._bind.pop(server_id, None)
        except ProviderError as e:
            log.info("AssociateAddress %s → %s: %s (will retry)", eid, server_id, e)

    async def wait_ip(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=False)

    async def wait_active(self, region: str, server_id: str, timeout: int) -> Server:
        return await self._wait(region, server_id, timeout, need_active=True)

    async def _wait(self, region: str, server_id: str, timeout: int, need_active: bool) -> Server:
        region = region or self._region_of.get(server_id, "")
        deadline = time.time() + timeout
        while True:
            s = await self.get_server(region, server_id)
            if s.status == "ACTIVE" and server_id in self._bind:
                await self._bind_eip(region, server_id)
                s = await self.get_server(region, server_id)
            if s.status == "ERROR":
                return s
            if s.ipv4 and (s.status == "ACTIVE" or not need_active) and server_id not in self._bind:
                return s
            if time.time() > deadline:
                s.status = "TIMEOUT"
                return s
            await asyncio.sleep(3)
