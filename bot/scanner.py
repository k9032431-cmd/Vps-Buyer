"""IP scouting for providers without a subnet API (Selectel VDS).

Creates the cheapest server, notes the public IP it gets as soon as it appears, deletes it and moves
on to the next one (one at a time by default, SCAN_PARALLEL to change). The /24 blocks seen are reported and remembered, so the IP step of the
purchase can offer them."""
import asyncio
import html
import logging
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .providers import Flavor, ProviderError
from .providers.base import prefix24

log = logging.getLogger(__name__)
BUSY_TRIES = 20   # tries of one probe while the account is at its limit
BUSY_WAIT = 15


@dataclass
class ScanJob:
    region: str
    flavor: Flavor
    image_id: str
    samples: int
    parallel: int = 1          # one at a time: create → note IP → delete → next
    ip_timeout: int = 300
    created: int = 0
    deleted: int = 0
    ips: list[str] = field(default_factory=list)
    subnets: list[str] = field(default_factory=list)  # CIDRs, when the provider reports masks
    log: list[str] = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None
    leftovers: list[str] = field(default_factory=list)  # servers we could not delete
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)  # the running scan
    finished_probes: int = 0
    no_ports: bool = False     # the provider refused IP-only probes: buy servers instead
    waiting: bool = False      # a probe is waiting for the account limit

    def eta_seconds(self) -> float | None:
        if not self.finished_probes:
            return None
        per_probe = (time.time() - self.started) / self.finished_probes  # wall time, parallelism included
        return per_probe * (self.samples - self.finished_probes)

    def blocks(self) -> list[tuple[str, int]]:
        return Counter(prefix24(ip) for ip in self.ips).most_common()


def _fatal(e: BaseException) -> bool:
    return isinstance(e, ProviderError) and e.status is not None and 400 <= e.status < 500 and e.status != 429


async def _probe(client, job: ScanJob) -> None:
    if job.cancelled or job.error:
        return
    ip_only = getattr(client, "reserves_ips", False) or getattr(client, "ports_anywhere", False)
    if ip_only and not job.no_ports:
        # Hetzner / Vultr / OVH: take a standalone IP (port), note it, release it — no server is bought
        try:
            port_id, ip = await client.create_port(job.region, None, None)
        except ProviderError as e:
            if getattr(client, "reserves_ips", False) or e.status == 429:
                raise
            job.no_ports = True  # OVH refuses ports on its public network → probe with servers
            job.log.append("ℹ️ IP без сервера взять нельзя — разведываю через серверы")
            return await _probe(client, job)
        job.created += 1
        try:
            if ip:
                job.ips.append(ip)
                job.log.append(f"🔎 <code>{html.escape(ip)}</code>")
                cidr = getattr(client, "cidr_of", {}).get(ip)  # Gcore: the IP's whole subnet
                if cidr and cidr not in job.subnets:
                    job.subnets.append(cidr)
        finally:
            await client.delete_port(job.region, port_id)
            job.deleted += 1
        return
    srv = await client.create_server(job.region, f"ipscan-{secrets.token_hex(3)}", job.flavor, job.image_id,
                                     None, 10)
    job.created += 1
    try:
        state = await client.wait_ip(job.region, srv.id, job.ip_timeout)
        if state.ipv4:
            job.ips += state.ipv4
            job.subnets += [c for c in state.cidrs if c not in job.subnets]
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
            for attempt in range(BUSY_TRIES):
                try:
                    await _probe(client, job)
                    break
                except Exception as e:
                    busy = isinstance(e, ProviderError) and e.status == 429
                    if busy and attempt < BUSY_TRIES - 1 and not job.cancelled:
                        # account limit (servers still being deleted) or rate limit: wait, don't lose the probe
                        if not job.waiting:
                            job.log.append("⏳ Лимит аккаунта — жду, пока удалятся прошлые серверы")
                        job.waiting = True
                        await asyncio.sleep(BUSY_WAIT)
                        continue
                    job.log.append(f"⚠️ {html.escape(str(e))[:200]}")
                    if _fatal(e):
                        job.error = str(e)
                    break
            job.waiting = False
            job.finished_probes += 1
            await on_progress(job)

    await asyncio.gather(*(one() for _ in range(job.samples)))
    return job
