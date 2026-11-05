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


@dataclass
class Image:
    id: str
    name: str


@dataclass
class IpPool:
    """Group of public IPv4 subnets that share the same first octet (e.g. 94.x.x.x)."""
    prefix: str
    network_id: str | None
    cidrs: list[str]


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
