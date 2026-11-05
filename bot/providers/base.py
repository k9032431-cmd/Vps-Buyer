"""Types shared by all providers. Each provider client implements the same async methods:

    async with Client(...) as c:
        await c.check() -> str                      # validate credentials, short account info
        await c.region_statuses() -> [RegionStatus]
        await c.ip_pools(region) -> [IpPool]
        await c.list_flavors(region) -> [Flavor]
        await c.list_images(region, flavor) -> [Image]
        await c.create_server(region, name, flavor, image_id, network_id, boot_volume_gb) -> Server
        await c.wait_active(region, server_id, timeout) -> Server
        await c.delete_server(region, server_id)
"""
import ipaddress
from dataclasses import dataclass, field


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Flavor:
    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int
    price: str = ""
    kind: str = ""
    network_plans: list[dict] = field(default_factory=list)   # HostVDS: [{name, title, price, …}]
    default_network_plan: str = ""
    currency: str = "usd"


@dataclass
class Image:
    id: str
    name: str


@dataclass
class IpPool:
    """A /24 block of public IPv4 addresses, e.g. prefix "95.182.87" = 95.182.87.#."""
    prefix: str
    network_id: str | None
    cidrs: list[str]                 # source subnets (or example IPs when learned from servers)
    subnet_id: str | None = None     # allocate straight from this subnet when the provider allows it
    free: int | None = None          # free IPv4 addresses in its network, when the provider tells us


def prefix24(ip: str) -> str:
    return ip.rsplit(".", 1)[0]


def blocks24(cidr: str, allocation_pools: list[dict] | None = None, limit: int = 256) -> list[str]:
    """All /24 prefixes ("a.b.c") that contain allocatable addresses of a subnet."""
    net = ipaddress.ip_network(cidr, strict=False)
    ranges = [(ipaddress.ip_address(p["start"]), ipaddress.ip_address(p["end"])) for p in allocation_pools or []]
    if not ranges:
        ranges = [(net.network_address, net.broadcast_address)]
    result: list[str] = []
    for start, end in ranges:
        block = int(start) & ~0xFF
        while block <= int(end) and len(result) < limit:
            prefix = prefix24(str(ipaddress.ip_address(block)))
            if prefix not in result:
                result.append(prefix)
            block += 256
    return result


@dataclass
class RegionStatus:
    region: str
    available: bool
    reason: str
    sample_ip: str | None
    pools: list[IpPool] = field(default_factory=list)
    country: str = ""   # known country/city (skips IP geolocation)
    city: str = ""


@dataclass
class Server:
    id: str
    name: str
    status: str
    ipv4: list[str]
    admin_pass: str | None = None
