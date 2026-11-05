"""Turns raw cloud image names ("ubuntu-22.04-x86_64-2024-05", "Debian_12_64_001_master") into a short,
clean OS list: one button per OS version ("🟠 Ubuntu 22.04"), newest first."""
import re
from dataclasses import dataclass

from .providers import Image

# order = order of buttons
FAMILIES = [
    ("ubuntu", "Ubuntu", "🟠"),
    ("debian", "Debian", "🔴"),
    ("alma", "AlmaLinux", "🔵"),
    ("rocky", "Rocky Linux", "🟢"),
    ("centos", "CentOS", "🟣"),
    ("fedora", "Fedora", "🔷"),
    ("oracle", "Oracle Linux", "🟥"),
    ("opensuse", "openSUSE", "🦎"),
    ("arch", "Arch Linux", "🔹"),
    ("freebsd", "FreeBSD", "😈"),
    ("windows", "Windows Server", "🪟"),
]
# images for apps/panels/etc. are hidden from the main list
APP_WORDS = ("docker", "wordpress", "lamp", "lemp", "panel", "plesk", "cpanel", "hestia", "bitrix",
             "gitlab", "nextcloud", "vpn", "minecraft", "rescue", "test", "snapshot", "backup", "1c",
             "fastpanel", "ispmanager", "openvpn", "wireguard", "outline", "marzban", "k8s", "kube")
STALE_RE = re.compile(r"(?<![a-z])(old|deprecated|legacy|obsolete)(?![a-z])")
VERSION_RE = re.compile(r"(?<![\d.])(\d{1,4}(?:\.\d{1,2})?)(?![\d])")


@dataclass
class OsChoice:
    id: str        # image id
    title: str     # "🟠 Ubuntu 22.04"
    raw: str       # original image name
    distro: str = ""   # "ubuntu", "debian"… (HostVDS panel sends it as server metadata os_distro)


def _version(name: str, family_key: str) -> str:
    tail = name.lower().split(family_key, 1)[1]
    for m in VERSION_RE.finditer(tail):
        v = m.group(1)
        if v in ("32", "64", "86") or (len(v) == 4 and v.startswith("20") and family_key != "windows"):
            continue  # architecture / build year
        return v
    return ""


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".") if x.isdigit()) if v else (0,)


def simplify(images: list[Image]) -> tuple[list[OsChoice], list[Image]]:
    """Returns (clean OS choices, everything else)."""
    best: dict[tuple[str, str], OsChoice] = {}
    other: list[Image] = []
    for img in images:
        low = img.name.lower()
        fam = next((f for f in FAMILIES if f[0] in low), None)
        if not fam or any(w in low for w in APP_WORDS) or STALE_RE.search(low):
            other.append(img)
            continue
        key, title, icon = fam
        ver = _version(img.name, key)
        label = f"{icon} {title} {ver}".strip()
        slot = (key, ver)
        # several builds of the same version → keep the newest-looking one (latest name sorts last)
        if slot not in best or img.name > best[slot].raw:
            best[slot] = OsChoice(img.id, label, img.name, key)
    order = {f[0]: i for i, f in enumerate(FAMILIES)}
    choices = sorted(best.items(), key=lambda kv: (order[kv[0][0]], tuple(-x for x in _vkey(kv[0][1]))))
    return [c for _, c in choices], sorted(other, key=lambda i: i.name.lower())
