"""Purchase loop: get the requested number of servers whose IP is in the chosen block.

Fast mode (HostVDS / OpenStack): IPs are hunted with network ports — creating a port is free and takes
about a second. Ports with a wrong IP are held until the end (so the cloud does not hand the same IP
back) and then released. Servers are bought only on ports that already have the right IP.

Fallback mode (no ports, e.g. Selectel): create a server, look at its IP as soon as it appears (before
it finishes booting), delete it right away if the IP is wrong.
"""
import asyncio
import html
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .providers import Flavor, ProviderError, Server

HELD_PORTS_LIMIT = 200


# Client errors (bad request, no money, quota, forbidden…) stop the job; 409/429 are transient.
def _fatal(status: int | None) -> bool:
    return status is not None and 400 <= status < 500 and status not in (409, 429)


@dataclass
class PurchaseJob:
    region: str
    flavor: Flavor
    image_id: str
    network_id: str | None
    prefixes: list[str]          # wanted IP blocks, e.g. ["95.182.87", "94.103.80"] — any of them
    count: int
    max_attempts: int
    parallel: int
    active_timeout: int
    boot_volume_gb: int
    targets: list[tuple[str | None, str | None]] = field(default_factory=list)  # (network, subnet) per prefix
    on_found: Callable[[Server], Awaitable[None]] | None = None   # called as soon as a server is ready
    max_port_attempts: int = 500
    port_parallel: int = 10
    kept: list[Server] = field(default_factory=list)
    attempts: int = 0            # servers created
    deleted: int = 0             # servers deleted
    ports_checked: int = 0
    mode: str = ""               # "ports" | "servers"
    log: list[str] = field(default_factory=list)   # HTML-safe lines
    seen: list[str] = field(default_factory=list)  # every IP the provider gave us
    cancelled: bool = False
    error: str | None = None


def ip_matches(ip: str, prefixes: str | list[str]) -> bool:
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    for prefix in prefixes:
        prefix = prefix.rstrip(".#").rstrip(".")
        if ip == prefix or ip.startswith(prefix + "."):
            return True
    return False


def _port_targets(job: "PurchaseJob") -> list[tuple[str, str | None]]:
    """Distinct (network, subnet) pairs to hunt ports on; a network-wide target makes subnets redundant."""
    targets = [(n, s) for n, s in job.targets if n] or ([(job.network_id, None)] if job.network_id else [])
    return list(dict.fromkeys(targets))


async def _found(job: "PurchaseJob", server: Server) -> None:
    job.kept.append(server)
    if job.on_found:
        try:
            await job.on_found(server)
        except Exception:
            pass


def _code(ip: str) -> str:
    return f"<code>{html.escape(ip)}</code>"


def _err(e: BaseException) -> str:
    return html.escape(str(e) or type(e).__name__)[:300]


def _note_error(job: PurchaseJob, e: BaseException) -> None:
    job.log.append(f"⚠️ {_err(e)}")
    if isinstance(e, ProviderError) and _fatal(e.status):
        job.error = str(e)


# ---------------- fast mode: ports ----------------
async def _hunt_ports(client, job: PurchaseJob, progress) -> list[tuple[str, str]] | None:
    """Returns [(port_id, ip)] with matching IPs, or None if ports are not allowed for this account."""
    good: list[tuple[str, str]] = []
    held: list[str] = []
    try:
        while len(good) < job.count and job.ports_checked < job.max_port_attempts and not job.cancelled:
            batch = min(job.port_parallel, job.max_port_attempts - job.ports_checked)
            first = job.ports_checked == 0
            targets = _port_targets(job)
            results = await asyncio.gather(
                *(client.create_port(job.region, *targets[(job.ports_checked + i) % len(targets)])
                  for i in range(batch)),
                return_exceptions=True)
            job.ports_checked += batch
            if first and all(isinstance(r, BaseException) for r in results):
                return None  # ports are not available for this account → buy servers instead
            for r in results:
                if isinstance(r, BaseException):
                    _note_error(job, r)
                    continue
                port_id, ip = r
                if ip:
                    job.seen.append(ip)
                if ip and ip_matches(ip, job.prefixes) and len(good) < job.count:
                    good.append((port_id, ip))
                    job.log.append(f"🎯 {_code(ip)} — найден")
                else:
                    held.append(port_id)
            if len(held) > HELD_PORTS_LIMIT:
                old, held = held[:HELD_PORTS_LIMIT // 2], held[HELD_PORTS_LIMIT // 2:]
                await asyncio.gather(*(client.delete_port(job.region, p) for p in old), return_exceptions=True)
            await progress(job)
            if job.error:
                break
    finally:
        await asyncio.gather(*(client.delete_port(job.region, p) for p in held), return_exceptions=True)
    if job.cancelled or job.error:
        await asyncio.gather(*(client.delete_port(job.region, p) for p, _ in good), return_exceptions=True)
        return []
    return good


async def _server_on_port(client, job: PurchaseJob, port_id: str, ip: str) -> None:
    name = f"vps-{secrets.token_hex(3)}"
    job.attempts += 1
    try:
        srv = await client.create_server(job.region, name, job.flavor, job.image_id, job.network_id,
                                         job.boot_volume_gb, port_id=port_id)
    except BaseException:
        await client.delete_port(job.region, port_id)
        raise
    state = await client.wait_active(job.region, srv.id, job.active_timeout)
    if state.status == "ACTIVE":
        state.admin_pass = srv.admin_pass
        state.ipv4 = [ip] + [x for x in state.ipv4 if x != ip]
        job.log.append(f"✅ {_code(ip)} — сервер готов")
        await _found(job, state)
        return
    await client.delete_server(job.region, srv.id)  # also releases the port
    job.deleted += 1
    job.log.append(f"⚠️ {_code(ip)} — сервер не запустился ({state.status}), удалён")


# ---------------- fallback mode: servers ----------------
async def _try_server(client, job: PurchaseJob) -> None:
    name = f"vps-{secrets.token_hex(3)}"
    srv = await client.create_server(job.region, name, job.flavor, job.image_id, job.network_id, job.boot_volume_gb)
    try:
        state = await client.wait_ip(job.region, srv.id, job.active_timeout)
        job.seen += state.ipv4
        good = next((ip for ip in state.ipv4 if ip_matches(ip, job.prefixes)), None)
        if good and state.status != "ERROR" and len(job.kept) < job.count and not job.cancelled:
            if state.status != "ACTIVE":
                state = await client.wait_active(job.region, srv.id, job.active_timeout)
            if state.status == "ACTIVE":
                state.admin_pass = srv.admin_pass
                state.ipv4 = [good] + [ip for ip in state.ipv4 if ip != good]
                job.log.append(f"✅ {_code(good)} — оставлен")
                await _found(job, state)
                return
    except BaseException:
        await client.delete_server(job.region, srv.id)
        job.deleted += 1
        raise
    await client.delete_server(job.region, srv.id)
    job.deleted += 1
    ips = ", ".join(_code(ip) for ip in state.ipv4) or "без IP"
    job.log.append(f"🗑 {ips} — удалён")


async def run_purchase(client, job: PurchaseJob,
                       on_progress: Callable[[PurchaseJob], Awaitable[None]]) -> PurchaseJob:
    if getattr(client, "supports_ports", False) and _port_targets(job):
        job.mode = "ports"
        good = await _hunt_ports(client, job, on_progress)
        if good is None:
            job.log.append("ℹ️ Быстрый подбор через порты недоступен — покупаю серверы")
        else:
            results = await asyncio.gather(*(_server_on_port(client, job, p, ip) for p, ip in good),
                                           return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    _note_error(job, r)
            await on_progress(job)
            return job

    job.mode = "servers"
    while len(job.kept) < job.count and job.attempts < job.max_attempts and not job.cancelled:
        need = job.count - len(job.kept)
        batch = min(need, job.parallel, job.max_attempts - job.attempts)
        job.attempts += batch
        results = await asyncio.gather(*(_try_server(client, job) for _ in range(batch)), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                _note_error(job, r)
        await on_progress(job)
        if job.error:
            break
    return job
