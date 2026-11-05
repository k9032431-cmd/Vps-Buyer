"""Purchase loop: create servers until the requested number get an IP from the chosen pool."""
import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .providers import Flavor, ProviderError, Server

# Client errors (bad request, no money, quota, forbidden…) stop the job; 409/429 are transient.
def _fatal(status: int | None) -> bool:
    return status is not None and 400 <= status < 500 and status not in (409, 429)


@dataclass
class PurchaseJob:
    region: str
    flavor: Flavor
    image_id: str
    network_id: str | None
    prefix: str
    count: int
    max_attempts: int
    parallel: int
    active_timeout: int
    boot_volume_gb: int
    kept: list[Server] = field(default_factory=list)
    attempts: int = 0
    deleted: int = 0
    log: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)   # every IP the provider gave us
    cancelled: bool = False
    error: str | None = None


def ip_matches(ip: str, prefix: str) -> bool:
    return ip == prefix or ip.startswith(prefix.rstrip(".") + ".")


async def _try_one(client, job: PurchaseJob) -> None:
    name = f"vps-{secrets.token_hex(3)}"
    srv = await client.create_server(job.region, name, job.flavor, job.image_id, job.network_id, job.boot_volume_gb)
    try:
        state = await client.wait_active(job.region, srv.id, job.active_timeout)
    except BaseException:
        await client.delete_server(job.region, srv.id)
        raise
    job.seen += state.ipv4
    good = next((ip for ip in state.ipv4 if ip_matches(ip, job.prefix)), None)
    if state.status == "ACTIVE" and good and len(job.kept) < job.count and not job.cancelled:
        state.admin_pass = srv.admin_pass
        state.ipv4 = [good] + [ip for ip in state.ipv4 if ip != good]
        job.kept.append(state)
        job.log.append(f"✅ {good} — оставлен")
        return
    await client.delete_server(job.region, srv.id)
    job.deleted += 1
    ip = ", ".join(state.ipv4) or "без IP"
    job.log.append(f"🗑 {ip} ({state.status}) — удалён")


async def run_purchase(client, job: PurchaseJob,
                       on_progress: Callable[[PurchaseJob], Awaitable[None]]) -> PurchaseJob:
    while len(job.kept) < job.count and job.attempts < job.max_attempts and not job.cancelled:
        need = job.count - len(job.kept)
        batch = min(need, job.parallel, job.max_attempts - job.attempts)
        job.attempts += batch
        results = await asyncio.gather(*(_try_one(client, job) for _ in range(batch)), return_exceptions=True)
        for r in results:
            if isinstance(r, ProviderError):
                job.log.append(f"⚠️ {r}")
                if _fatal(r.status):
                    job.error = str(r)
            elif isinstance(r, BaseException):
                job.log.append(f"⚠️ {type(r).__name__}: {r}")
        await on_progress(job)
        if job.error:
            break
    return job
