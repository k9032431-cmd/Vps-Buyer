""""Only working" check: log in to a fresh server over SSH and fetch a page from it.
The server counts as working when the page's HTML comes back."""
import asyncio
import logging
import shlex
import time

import asyncssh

log = logging.getLogger(__name__)

HTML_MARKERS = ("<html", "<!doctype html")


def target(ip: str) -> tuple[str, int]:
    """Where to connect for this IP (tests point it at a local SSH server)."""
    return ip, 22


def fetch_command(url: str) -> str:
    u = shlex.quote(url)
    return (f"(curl -sSL -m 25 {u} || wget -qO- -T 25 {u} || "
            f"python3 -c \"import urllib.request,sys;sys.stdout.write(urllib.request.urlopen({url!r},timeout=25)"
            f".read().decode('utf-8','replace'))\") 2>/dev/null | head -c 200000")


def looks_like_html(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in HTML_MARKERS)


async def check_server(ip: str, url: str, password: str | None = None, key_file: str | None = None,
                       ssh_timeout: int = 300) -> tuple[bool | None, str]:
    """(True, "") — the page opened; (False, why) — it did not; (None, why) — could not log in at all."""
    host, port = target(ip)
    deadline = time.time() + ssh_timeout
    last_error = "SSH не ответил"
    while time.time() < deadline:
        try:
            async with asyncssh.connect(
                    host, port=port, username="root", password=password,
                    client_keys=[key_file] if key_file else None, known_hosts=None,
                    connect_timeout=15, login_timeout=20, keepalive_interval=10) as conn:
                res = await asyncio.wait_for(conn.run(fetch_command(url), check=False), 90)
                body = res.stdout if isinstance(res.stdout, str) else (res.stdout or b"").decode("utf-8", "replace")
                if looks_like_html(body):
                    return True, ""
                return False, "страница не открылась" if not body.strip() else "ответ без HTML"
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
            # booting, sshd not up yet, our key / password not applied yet — try again shortly
            last_error = f"SSH: {type(e).__name__}"
            await asyncio.sleep(10)
    return None, last_error
