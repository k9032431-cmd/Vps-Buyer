"""IP scouting for providers without a subnet API (Selectel VDS).

Creates a few of the cheapest servers, notes the public IP each one gets as soon as it appears and
deletes the server right away. The /24 blocks seen are reported and remembered, so the IP step of the
purchase can offer them."""
import asyncio
import html
import logging
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .providers import Flavor, ProviderError
from .providers.base import prefix24

log = logging.getLogger(__name__)


@dataclass
class ScanJob:
    region: str
    flavor: Flavor
    image_id: str
    samples: int
    parallel: int = 3
    ip_timeout: int = 300
    created: int = 0
    deleted: int = 0
    ips: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None
    leftovers: list[str] = field(default_factory=list)  # servers we could not delete

    def blocks(self) -> list[tuple[str, int]]:
        return Counter(prefix24(ip) for ip in self.ips).most_common()


def _fatal(e: BaseException) -> bool:
    return isinstance(e, ProviderError) and e.status is not None and 400 <= e.status < 500 and e.status != 429


async def _probe(client, job: ScanJob) -> None:
    if job.cancelled or job.error:
        return
    srv = await client.create_server(job.region, f"ipscan-{secrets.token_hex(3)}", job.flavor, job.image_id,
                                     None, 10)
    job.created += 1
    try:
        state = await client.wait_ip(job.region, srv.id, job.ip_timeout)
        if state.ipv4:
            job.ips += state.ipv4
            job.log.append("🔎 " + ", ".join(f"<code>{html.escape(ip)}</code>" for ip in state.ipv4))
        else:
            job.log.append(f"⚠️ сервер не получил IP ({html.escape(state.status)})")
    finally:
        try:
            await client.delete_server(job.region, srv.id)
            job.deleted += 1
        except Exception as e:
            job.leftovers.append(srv.id)
            log.exception("scan: could not delete %s", srv.id)
            job.log.append(f"❗ не удалось удалить сервер {html.escape(srv.id)}: {html.escape(str(e))[:120]}")


async def run_scan(client, job: ScanJob, on_progress: Callable[[ScanJob], Awaitable[None]]) -> ScanJob:
    sem = asyncio.Semaphore(job.parallel)

    async def one():
        async with sem:
            if job.cancelled or job.error:
                return
            try:
                await _probe(client, job)
            except Exception as e:
                job.log.append(f"⚠️ {html.escape(str(e))[:200]}")
                if _fatal(e):
                    job.error = str(e)
            await on_progress(job)

    await asyncio.gather(*(one() for _ in range(job.samples)))
    return job
