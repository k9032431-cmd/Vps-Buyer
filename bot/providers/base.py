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
import base64
import binascii
import ipaddress
import shlex
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
    hits: int = 0                    # how many IPs from it this datacenter already handed out to us


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


def subnet_blocks(cidr: str, max_blocks: int = 16) -> list[str]:
    """/24 blocks of a subnet learned from an IP's mask. Wide subnets (> /20) are not expanded:
    the provider is unlikely to hand out every block of them."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > 256 * max_blocks:
        return []
    return blocks24(cidr)


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
    cidrs: list[str] = field(default_factory=list)   # public subnets of its IPs, when the provider tells
    finalized: bool = False                          # provider-specific last step already done
    region: str = ""                                 # filled by list_servers()


SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                 "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-dss")


def normalize_ssh_key(text: str) -> str | None:
    """A single-line OpenSSH public key ("ssh-ed25519 AAAA… comment") or None if it is not one."""
    parts = " ".join((text or "").split()).split(" ")
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 32 or parts[0].split("@")[0].encode() not in blob[:64]:
        return None
    return " ".join(parts[:3])


def ssh_key_script(public_keys: str | list[str]) -> str:
    """First-boot script that lets root log in with these keys (for user_data / cloud_init style fields)."""
    keys = [public_keys] if isinstance(public_keys, str) else list(public_keys)
    return "\n".join([
        "#!/bin/bash",
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh",
        *(f"printf '%s\\n' {shlex.quote(k)} >> /root/.ssh/authorized_keys" for k in keys),
        "chmod 600 /root/.ssh/authorized_keys",
    ])


def merge_scripts(*scripts: str | None) -> str:
    """Join several "#!/bin/bash" scripts into one (user_data takes a single script)."""
    body = []
    for s in scripts:
        if s:
            body += [line for line in s.splitlines() if not line.startswith("#!")]
    return "\n".join(["#!/bin/bash", *body])


def open_access_script(password_login: bool) -> str:
    """First-boot script: switch off the OS firewall (ufw) so every port is reachable and, for password
    servers, make sure root can log in with a password — some images (DigitalOcean with SSH keys in the
    order, cloud-init's 50-cloud-init.conf…) turn password logins off. Runs after cloud-init is done."""
    lines = ["#!/bin/bash", "(sleep 20", "ufw disable >/dev/null 2>&1 || true"]
    if password_login:
        lines += [
            "mkdir -p /etc/ssh/sshd_config.d",
            "printf 'PasswordAuthentication yes\\nKbdInteractiveAuthentication yes\\nPermitRootLogin yes\\n' "
            "> /etc/ssh/sshd_config.d/00-vps-buyer.conf",
            "sed -i -E 's/^#?[[:space:]]*(PasswordAuthentication|PermitRootLogin)[[:space:]].*/\\1 yes/' "
            "/etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null",
            "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null",
        ]
    lines.append(")&")
    return "\n".join(lines)
