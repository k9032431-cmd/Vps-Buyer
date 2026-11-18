"""Public geofeeds (RFC 8805): "subnet,country,region,city,zip" lines that providers publish for their IPs.
Used to list a datacenter's subnets before buying (DigitalOcean, Hetzner, Vultr, Linode)."""
import csv
import io
import ipaddress
import time

import aiohttp

TTL = 24 * 3600
_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}


async def load(url: str) -> list[tuple[str, str]]:
    """[(cidr, city)] for every public IPv4 subnet in the feed; cached for a day per URL."""
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            text = await r.text(errors="replace")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or row[0].startswith("#") or ":" in row[0]:
            continue
        try:
            rows.append((str(ipaddress.ip_network(row[0].strip(), strict=False)), row[3].strip()))
        except ValueError:
            continue
    _CACHE[url] = (time.time(), rows)
    return rows


async def subnets(url: str, cities: set[str]) -> list[str]:
    """Subnets of these cities (case-insensitive), sorted by address."""
    wanted = {c.lower() for c in cities}
    nets = {cidr for cidr, city in await load(url) if city.lower() in wanted}
    return sorted(nets, key=lambda c: ipaddress.ip_network(c))
