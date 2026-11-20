"""Purchase loop: get the requested number of servers whose IP is in the chosen block.

Fast mode (HostVDS / OpenStack): IPs are hunted with network ports — creating a port is free and takes
about a second. Ports with a wrong IP are held until the end (so the cloud does not hand the same IP
back) and then released. Servers are bought only on ports that already have the right IP.

Fallback mode (no ports, e.g. Selectel): create a server, look at its IP as soon as it appears (before
it finishes booting), delete it right away if the IP is wrong.
"""
import asyncio
import html
import ipaddress
import logging
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .providers import Flavor, ProviderError, Server

HELD_PORTS_LIMIT = 200
MAX_BATCH = 10  # most servers created at once when the provider reports free capacity
SLOT_WAIT = 180  # seconds to wait for capacity freed by our own deletions
START_SLOT_WAIT = 60  # same, before the first batch
NO_MATCH_HINT = 30  # attempts without a single match before the bot suggests other subnets
# Reserved IPs (Vultr, Hetzner, Civo) may come from other ranges than servers: after this many reserved
# IPs with none in the chosen subnets the bot goes on with servers instead.
RESERVE_NO_MATCH = 50
BUSY_WAIT = 15  # seconds between tries while the provider is busy or the account is at its limit
BUSY_ROUNDS_LIMIT = 20  # server rounds in a row refused by an account limit before the purchase stops
SERVER_BUSY_RETRIES = 12  # tries to build a server on a found IP while the account is at its limit


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
    metadata: dict = field(default_factory=dict)                  # server metadata (HostVDS network plan…)
    ssh_key: str | None = None                                    # log in with this public key instead of a password
    ssh_key_name: str = ""
    hinted: bool = False
    # "only working" mode: the bot logs in over SSH and opens this page from the server
    check_url: str | None = None
    bot_key: tuple[str, str] | None = None       # (private key file, public key line) of the bot
    check_timeout: int = 300                     # how long to wait for SSH on a fresh server
    checked: int = 0                             # servers checked over SSH
    # check results per /24 block: {"95.182.87": [passed, failed]} — earlier purchases + this one
    block_stats: dict = field(default_factory=dict)
    check_results: list = field(default_factory=list)  # (block, passed) found in this purchase
    skipped_bad: int = 0                         # servers / IPs dropped at once because their block fails
    max_port_attempts: int = 0   # 0 = no limit
    port_parallel: int = 10
    kept: list[Server] = field(default_factory=list)
    attempts: int = 0            # servers created
    deleted: int = 0             # servers deleted
    ports_checked: int = 0
    mode: str = ""               # "ports" | "servers"
    port_matches: int = 0        # reserved IPs / ports that were in the chosen subnets
    switch_reason: str = ""      # why IP hunting gave way to buying servers
    log: list[str] = field(default_factory=list)   # HTML-safe lines
    seen: list[str] = field(default_factory=list)  # every IP the provider gave us
    subnets: list[str] = field(default_factory=list)  # their subnets (CIDR), when the provider reports masks
    cancelled: bool = False
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)  # the running purchase


BAD_BLOCK_FAILS = 2  # a /24 with this many failed checks and no pass is skipped without checking


def block_is_bad(job: "PurchaseJob", ip: str) -> bool:
    if not job.check_url:
        return False
    passed, failed = job.block_stats.get(ip.rsplit(".", 1)[0], (0, 0))
    return passed == 0 and failed >= BAD_BLOCK_FAILS


def _remember_check(job: "PurchaseJob", ip: str, passed: bool) -> None:
    block = ip.rsplit(".", 1)[0]
    stats = job.block_stats.setdefault(block, [0, 0])
    stats[0 if passed else 1] += 1
    job.check_results.append((block, passed))


def ip_matches(ip: str, prefixes: str | list[str]) -> bool:
    """A prefix is either the start of an address ("95.182.87") or a subnet with a mask ("142.93.96.0/20")."""
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    for prefix in prefixes:
        if "/" in prefix:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(prefix, strict=False):
                    return True
            except ValueError:
                pass
            continue
        prefix = prefix.rstrip(".#").rstrip(".")
        if ip == prefix or ip.startswith(prefix + "."):
            return True
    return False


def _reserve_targets(client, job: "PurchaseJob") -> list[tuple[str | None, str | None]]:
    """Where to hunt IPs before buying: OpenStack ports on chosen networks (HostVDS), or — for providers that
    reserve a standalone IP and build a server on it (Hetzner Primary IP, Vultr Reserved IP) — the region."""
    if not getattr(client, "supports_ports", False):
        return []
    if getattr(client, "reserves_ips", False):
        return [(None, None)]
    return _port_targets(job)


def _port_targets(job: "PurchaseJob") -> list[tuple[str, str | None]]:
    """Distinct (network, subnet) pairs to hunt ports on; a network-wide target makes subnets redundant."""
    targets = [(n, s) for n, s in job.targets if n] or ([(job.network_id, None)] if job.network_id else [])
    return list(dict.fromkeys(targets))


async def _finalize(client, job: "PurchaseJob", server: Server) -> None:
    """Provider-specific last step for a kept server (Aeza: set a known root password)."""
    if hasattr(client, "finalize"):
        try:
            server.admin_pass = await client.finalize(job.region, server.id) or server.admin_pass
        except Exception:
            logging.getLogger(__name__).exception("finalize failed for %s", server.id)


async def _found(job: "PurchaseJob", server: Server) -> None:
    job.kept.append(server)
    if job.on_found:
        try:
            await job.on_found(server)
        except Exception:
            logging.getLogger(__name__).exception("on_found failed for %s", server.ipv4)


def _code(ip: str) -> str:
    return f"<code>{html.escape(ip)}</code>"


def _err(e: BaseException) -> str:
    return html.escape(str(e) or type(e).__name__)[:300]


async def _delete(client, job: PurchaseJob, server_id: str, ip: str | None = None) -> None:
    """Delete a server we do not keep; a failed delete is reported but never stops the purchase."""
    try:
        await client.delete_server(job.region, server_id)
        job.deleted += 1
    except Exception as e:
        logging.getLogger(__name__).exception("delete failed for %s", server_id)
        what = _code(ip) if ip else html.escape(server_id)
        job.log.append(f"❗ {what} — не удалось удалить ({_err(e)[:150]}). Проверьте панель провайдера.")


def _note_error(job: PurchaseJob, e: BaseException) -> None:
    job.log.append(f"⚠️ {_err(e)}")
    if isinstance(e, ProviderError) and _fatal(e.status):
        job.error = str(e)


# ---------------- fast mode: ports ----------------
async def _hunt_ports(client, job: PurchaseJob, progress) -> list[tuple[str, str]] | None:
    """Returns [(port_id, ip)] with matching IPs, or None if ports are not allowed for this account."""
    good: list[tuple[str, str]] = []
    held: list[str] = []
    held_limit = getattr(client, "max_held_ports", HELD_PORTS_LIMIT)
    parallel = min(job.port_parallel, getattr(client, "reserve_parallel", job.port_parallel))
    reserve = getattr(client, "reserves_ips", False)
    limited = job.max_port_attempts > 0
    try:
        while (len(good) < job.count - len(job.kept) and not job.cancelled
               and (not limited or job.ports_checked < job.max_port_attempts)):
            if reserve and not good and job.port_matches == 0 and job.ports_checked >= RESERVE_NO_MATCH:
                job.switch_reason = (f"За {job.ports_checked} резервных IP ни одного из выбранных подсетей — "
                                     "дальше подбираю через серверы")
                return None
            batch = parallel if not limited else min(parallel, job.max_port_attempts - job.ports_checked)
            first = job.ports_checked == 0
            targets = _reserve_targets(client, job)
            results = await asyncio.gather(
                *(client.create_port(job.region, *targets[(job.ports_checked + i) % len(targets)])
                  for i in range(batch)),
                return_exceptions=True)
            job.ports_checked += batch
            if first and all(isinstance(r, BaseException) for r in results):
                job.switch_reason = ("Резервные IP недоступны для аккаунта" if reserve
                                     else "Быстрый подбор через порты недоступен") + " — подбираю через серверы"
                job.log.append(f"⚠️ {_err(results[0])}")
                return None  # ports are not available for this account → buy servers instead
            refused = [r for r in results if isinstance(r, ProviderError) and _fatal(r.status)]
            if reserve and refused:
                # usually the account's reserved IP quota (on Vultr deleted IPs still count for 24 h)
                job.log.append(f"⚠️ {_err(refused[0])}")
                job.switch_reason = "Лимит резервных IP аккаунта исчерпан — дальше подбираю через серверы"
                await asyncio.gather(*(client.delete_port(job.region, p) for p, _ in good), return_exceptions=True)
                good.clear()
                return None
            for r in results:
                if isinstance(r, BaseException):
                    _note_error(job, r)
                    continue
                port_id, ip = r
                if ip:
                    job.seen.append(ip)
                if ip and ip_matches(ip, job.prefixes):
                    job.port_matches += 1
                    if block_is_bad(job, ip):
                        job.skipped_bad += 1
                if (ip and ip_matches(ip, job.prefixes) and not block_is_bad(job, ip)
                        and len(good) < job.count - len(job.kept)):
                    good.append((port_id, ip))
                    job.log.append(f"🎯 {_code(ip)} — нашёлся, покупаю сервер")
                else:
                    held.append(port_id)
            if len(held) > held_limit:
                old, held = held[:max(len(held) - held_limit // 2, 1)], held[max(len(held) - held_limit // 2, 1):]
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
    for attempt in range(SERVER_BUSY_RETRIES):
        try:
            srv = await client.create_server(job.region, name, job.flavor, job.image_id, job.network_id,
                                             job.boot_volume_gb, port_id=port_id, metadata=job.metadata,
                                             ssh_key=job.ssh_key,
                                             extra_keys=[job.bot_key[1]] if job.bot_key else None)
            break
        except BaseException as e:
            busy = isinstance(e, ProviderError) and e.status == 429
            if busy and attempt < SERVER_BUSY_RETRIES - 1 and not job.cancelled:
                if attempt == 0:  # account limit: keep the good IP and wait for a free slot
                    job.log.append(f"⏳ {_code(ip)} — лимит аккаунта, жду свободное место для сервера")
                await asyncio.sleep(BUSY_WAIT)
                continue
            await client.delete_port(job.region, port_id)
            if busy:  # waited long enough: the account limit or the plan's stock is not freeing up
                raise ProviderError(f"Провайдер так и не дал создать сервер: {e}", 400) from e
            raise
    state = await client.wait_active(job.region, srv.id, job.active_timeout)
    reason = f"не запустился ({state.status})"
    if state.status == "ACTIVE":
        state.admin_pass = state.admin_pass or srv.admin_pass
        state.ipv4 = [ip] + [x for x in state.ipv4 if x != ip]
        works = True
        if job.check_url:
            job.log.append(f"🔎 {_code(ip)} — проверяю…")
            works, _why = await _check(client, job, state, ip)
            reason = "не прошёл проверку"
        if works and not job.cancelled:
            job.log.append(f"✅ {_code(ip)} — " + ("проверка пройдена, сервер рабочий" if job.check_url
                                                  else "сервер готов"))
            if not state.finalized:
                await _finalize(client, job, state)
            await _found(job, state)
            return
    await _delete(client, job, srv.id, ip)  # also releases the port
    job.log.append(f"🗑 {_code(ip)} — {reason}, удалён")


# ---------------- fallback mode: servers ----------------
async def _check(client, job: PurchaseJob, state: Server, ip: str) -> tuple[bool, str]:
    """"Only working" mode: log in over SSH and open the check page from the server."""
    from . import checker
    if not state.admin_pass and hasattr(client, "finalize"):  # Aeza: set a known password first
        await _finalize(client, job, state)
        state.finalized = True
    job.checked += 1
    ok, why = await checker.check_server(ip, job.check_url, password=state.admin_pass,
                                         key_file=job.bot_key[0] if job.bot_key else None,
                                         ssh_timeout=job.check_timeout)
    if not ok:
        logging.getLogger(__name__).info("check failed for %s: %s", ip, why)
    if ok is not None:  # an SSH problem says nothing about the IP itself
        _remember_check(job, ip, bool(ok))
    return bool(ok), why


async def _try_server(client, job: PurchaseJob) -> None:
    name = f"vps-{secrets.token_hex(3)}"
    srv = await client.create_server(job.region, name, job.flavor, job.image_id, job.network_id, job.boot_volume_gb,
                                     metadata=job.metadata, ssh_key=job.ssh_key,
                                     extra_keys=[job.bot_key[1]] if job.bot_key else None)
    reason = "не тот IP"
    try:
        state = await client.wait_ip(job.region, srv.id, job.active_timeout)
        job.seen += state.ipv4
        job.subnets += [c for c in state.cidrs if c not in job.subnets]
        if job.prefixes:
            good = next((ip for ip in state.ipv4 if ip_matches(ip, job.prefixes)), None)
        else:  # no subnets chosen ("only working" mode): any IP will do
            good = state.ipv4[0] if state.ipv4 else None
        if good and block_is_bad(job, good):
            good, reason = None, "блок не проходит проверку"
            job.skipped_bad += 1
        if good and state.status != "ERROR" and len(job.kept) < job.count and not job.cancelled:
            if state.status != "ACTIVE":
                state = await client.wait_active(job.region, srv.id, job.active_timeout)
            if state.status == "ACTIVE":
                state.admin_pass = state.admin_pass or srv.admin_pass
                state.ipv4 = [good] + [ip for ip in state.ipv4 if ip != good]
                works = True
                if block_is_bad(job, good):
                    works, reason = False, "блок не проходит проверку"
                    job.skipped_bad += 1
                elif job.check_url:
                    job.log.append(f"🔎 {_code(good)} — проверяю…")
                    works, _why = await _check(client, job, state, good)  # details stay in the bot's log
                    reason = "не прошёл проверку"
                if works and len(job.kept) < job.count and not job.cancelled:
                    job.log.append(f"✅ {_code(good)} — " + ("проверка пройдена, сервер рабочий" if job.check_url
                                                            else "подходит, оставлен"))
                    if not getattr(state, "finalized", False):
                        await _finalize(client, job, state)
                    await _found(job, state)
                    return
            else:
                reason = f"не запустился ({state.status})"
    except BaseException:
        await _delete(client, job, srv.id)
        raise
    await _delete(client, job, srv.id)
    ips = ", ".join(_code(ip) for ip in state.ipv4) or "без IP"
    job.log.append(f"🗑 {ips} — {html.escape(reason)}, удалён")


async def run_purchase(client, job: PurchaseJob,
                       on_progress: Callable[[PurchaseJob], Awaitable[None]]) -> PurchaseJob:
    if _reserve_targets(client, job) and job.prefixes:
        job.mode = "ports"
        # rounds: hunt IPs on free ports → buy servers on them → (check) — until enough servers are kept
        while len(job.kept) < job.count and not job.cancelled and not job.error:
            good = await _hunt_ports(client, job, on_progress)
            if good is None:  # no reserved IPs / ports for this account, or they never hit the subnets
                job.log.append(f"ℹ️ {html.escape(job.switch_reason or 'Подбираю через серверы')}")
                job.mode = "servers"
                break
            if not good:  # port limit reached or stopped
                break
            results = await asyncio.gather(*(_server_on_port(client, job, p, ip) for p, ip in good),
                                           return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    _note_error(job, r)
            await on_progress(job)
        if job.mode == "ports":
            await on_progress(job)
            return job

    job.mode = "servers"
    unlimited = job.max_attempts <= 0
    busy_rounds = 0
    while (len(job.kept) < job.count and (unlimited or job.attempts < job.max_attempts)
           and not job.cancelled):
        need = job.count - len(job.kept)
        batch = max(need, job.parallel)
        if hasattr(client, "free_slots"):  # account limit on servers at once (DigitalOcean)
            slots, limit = await client.free_slots()
            waited = 0
            # right after a batch, servers we just deleted may still be counted for a few seconds
            # (also at the start: a previous purchase may still be deleting its droplets)
            while slots <= 0 and waited < (SLOT_WAIT if job.attempts else START_SLOT_WAIT) and not job.cancelled:
                await asyncio.sleep(10)
                waited += 10
                slots, limit = await client.free_slots()
            if slots <= 0:
                job.error = (f"Лимит аккаунта: уже {limit - slots} из {limit} серверов. Удалите лишние "
                             "или попросите провайдера поднять лимит.")
                job.log.append(f"❗ {html.escape(job.error)}")
                break
            batch = min(batch, slots, MAX_BATCH)
        else:
            batch = min(need, job.parallel)
        if not unlimited:
            batch = min(batch, job.max_attempts - job.attempts)
        job.attempts += batch
        results = await asyncio.gather(*(_try_server(client, job) for _ in range(batch)), return_exceptions=True)
        busy = False
        for r in results:
            if isinstance(r, BaseException):
                _note_error(job, r)
                busy = busy or (isinstance(r, ProviderError) and r.status == 429)
        if (job.prefixes and not job.kept and not job.hinted and job.attempts >= NO_MATCH_HINT
                and not any(ip_matches(ip, job.prefixes) for ip in job.seen)):
            job.hinted = True
            job.log.append(f"💡 За {job.attempts} попыток ни одного IP из выбранных подсетей — похоже, этот "
                           "дата-центр их сейчас не выдаёт. Остановите покупку и выберите подсети с ⭐ "
                           "(те, что здесь реально выпадают).")
        await on_progress(job)
        if job.error:
            break
        busy_rounds = busy_rounds + 1 if busy and all(isinstance(r, BaseException) for r in results) else 0
        if busy_rounds >= BUSY_ROUNDS_LIMIT:
            job.error = ("Провайдер не даёт создать сервер (лимит аккаунта или тариф закончился в этой "
                         "локации). Удалите лишние серверы, выберите другой тариф или попросите провайдера "
                         "поднять лимит.")
            job.log.append(f"❗ {html.escape(job.error)}")
            break
        if busy:  # rate or capacity limit — give the provider a moment instead of hammering it
            await asyncio.sleep(BUSY_WAIT)
    return job
