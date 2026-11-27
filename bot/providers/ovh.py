"""OVHcloud Public Cloud — plain OpenStack (Keystone v3 at auth.cloud.ovh.net, Nova, Neutron, Glance),
so the HostVDS client does the work. Differences: no public tariff/price API (Nova flavors, approximate
prices), regions named GRA11, SBG5, DE1, WAW1…, the public network is "Ext-Net", the project is given by id.

Fast mode: a Neutron port on Ext-Net shows its public IP at once and a server is built on it; if OVH
refuses ports there, the bot falls back to buying servers (both are hourly)."""
import asyncio

from .base import Flavor, IpPool, ProviderError, RegionStatus, Server
from .hostvds import HostVDSClient

AUTH_URL = "https://auth.cloud.ovh.net"
PUBLIC_NET = "Ext-Net"
# region prefix → (country, city)
CITIES = {
    "GRA": ("FR", "Гравлин"), "SBG": ("FR", "Страсбург"), "RBX": ("FR", "Рубе"), "EU-WEST-PAR": ("FR", "Париж"),
    "DE": ("DE", "Франкфурт"), "UK": ("GB", "Лондон"), "WAW": ("PL", "Варшава"), "EU-SOUTH-MIL": ("IT", "Милан"),
    "BHS": ("CA", "Богарнуа"), "CA-EAST-TOR": ("CA", "Торонто"), "SGP": ("SG", "Сингапур"),
    "SYD": ("AU", "Сидней"), "AP-SOUTH-MUM": ("IN", "Мумбаи"), "US-EAST-VA": ("US", "Вирджиния"),
    "US-WEST-OR": ("US", "Орегон"),
}
# small general-purpose flavors; approximate hourly price in € (OVH has no price API in OpenStack)
PRICES = {"d2-2": 0.0088, "d2-4": 0.0168, "d2-8": 0.0326, "b2-7": 0.0681, "b2-15": 0.1361, "b3-8": 0.0834,
          "b3-16": 0.1668, "c2-7": 0.1012, "c3-4": 0.0473, "c3-8": 0.0945, "s1-2": 0.0083, "s1-4": 0.0149,
          "s1-8": 0.0306}
SKIP = ("win-", "t1-", "t2-", "l4-", "l40s-", "a10-", "a100-", "h100-", "rtx", "i1-", "-flex")


def city(region: str) -> tuple[str, str]:
    for prefix in sorted(CITIES, key=len, reverse=True):
        if region.upper().startswith(prefix):
            return CITIES[prefix]
    return "", region


class OVHClient(HostVDSClient):
    ports_anywhere = True   # ports go on Ext-Net even when no subnet was chosen

    async def check(self) -> str:
        await self.authenticate()
        return "OVH, регионы: " + (", ".join(self.regions()) or "—")

    async def _free_ips(self, region: str) -> dict[str, int]:
        return {}

    async def region_statuses(self) -> list[RegionStatus]:
        await self._ensure_token()
        statuses = await asyncio.gather(*(self.region_status(r) for r in self.regions()))
        for st in statuses:
            st.country, st.city = city(st.region)
        return list(statuses)

    async def list_flavors(self, region: str) -> list[Flavor]:
        data = await self._request("GET", "compute", region, "/flavors/detail")
        flavors = []
        for f in data.get("flavors", []):
            name = f["name"]
            if any(s in name for s in SKIP) or not f.get("os-flavor-access:is_public", True):
                continue
            price = PRICES.get(name)
            flavors.append(Flavor(f["id"], name, f["vcpus"], f["ram"], f.get("disk", 0),
                                  f"≈€{price * 730:.0f}/мес" if price else ""))
        flavors.sort(key=lambda f: (PRICES.get(f.name, 99), f.vcpus, f.ram_mb))
        return flavors[:20]

    async def cheapest_plan(self, region: str) -> tuple[Flavor, float]:
        flavors = await self.list_flavors(region)
        if not flavors:
            raise ProviderError("В этой локации нет тарифов")
        return flavors[0], PRICES.get(flavors[0].name, 0.01) * 730

    async def _public_net(self, region: str) -> str:
        nets = (await self._request("GET", "network", region, "/networks", params={"name": PUBLIC_NET}))["networks"]
        if not nets:
            raise ProviderError(f"OVH: сеть {PUBLIC_NET} не найдена в {region}")
        return nets[0]["id"]

    async def ip_pools(self, region: str) -> list[IpPool]:
        try:
            return await super().ip_pools(region)  # Ext-Net subnets, when OVH shows them
        except ProviderError:
            return []

    async def create_port(self, region: str, network_id: str | None, subnet_id: str | None):
        return await super().create_port(region, network_id or await self._public_net(region), subnet_id)

    async def create_server(self, region: str, name: str, flavor: Flavor, image_id: str,
                            network_id: str | None, boot_volume_gb: int, port_id: str | None = None,
                            metadata: dict | None = None, ssh_key: str | None = None,
                            extra_keys: list[str] | None = None) -> Server:
        if not port_id and not network_id:
            network_id = await self._public_net(region)
        return await super().create_server(region, name, flavor, image_id, network_id, boot_volume_gb,
                                           port_id, None, ssh_key, extra_keys)

    async def get_server(self, region: str, server_id: str) -> Server:
        s = await super().get_server(region, server_id)
        s.ipv4 = [ip for ip in s.ipv4 if not ip.startswith(("10.", "192.168.", "172.16."))]
        return s
