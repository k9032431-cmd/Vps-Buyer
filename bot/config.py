import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    # Only these Telegram user IDs may use the bot (it spends real money).
    admin_ids: set[int] = field(default_factory=lambda: _ids(os.getenv("ADMIN_IDS", "")))
    # Telegram username people without access are sent to (no @).
    admin_contact: str = os.getenv("ADMIN_CONTACT", "oldarsi").lstrip("@")
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    # Fernet key used to encrypt stored provider passwords.
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    hostvds_auth_url: str = os.getenv("HOSTVDS_AUTH_URL", "https://os-api.hostvds.com/identity")
    hostvds_public_api: str = os.getenv("HOSTVDS_PUBLIC_API", "https://hostvds.com/api")
    selectel_api_url: str = os.getenv("SELECTEL_API_URL", "https://api.vscale.io/v1")
    timeweb_api_url: str = os.getenv("TIMEWEB_API_URL", "https://api.timeweb.cloud")
    aeza_api_url: str = os.getenv("AEZA_API_URL", "https://my.aeza.net/api")
    serverspace_api_url: str = os.getenv("SERVERSPACE_API_URL", "https://api.serverspace.ru")
    do_api_url: str = os.getenv("DO_API_URL", "https://api.digitalocean.com/v2")
    do_geofeed_url: str = os.getenv("DO_GEOFEED_URL", "https://www.digitalocean.com/geo/google.csv")
    hetzner_api_url: str = os.getenv("HETZNER_API_URL", "https://api.hetzner.cloud/v1")
    hetzner_geofeed_url: str = os.getenv("HETZNER_GEOFEED_URL", "https://www.hetzner.com/geofeed.csv")
    vultr_api_url: str = os.getenv("VULTR_API_URL", "https://api.vultr.com/v2")
    vultr_geofeed_url: str = os.getenv("VULTR_GEOFEED_URL", "https://geofeed.constant.com/")
    linode_api_url: str = os.getenv("LINODE_API_URL", "https://api.linode.com/v4")
    linode_geofeed_url: str = os.getenv("LINODE_GEOFEED_URL", "https://geoip.linode.com/")
    civo_api_url: str = os.getenv("CIVO_API_URL", "https://api.civo.com/v2")
    # Tencent Cloud API host (cvm.<host>, vpc.<host>)
    tencent_api_host: str = os.getenv("TENCENT_API_HOST", "tencentcloudapi.com")
    # Safety limit: max servers created in a single purchase job (incl. deleted ones).
    # 0 = no limit (stop with the ⛔ button).
    # (old MAX_ATTEMPTS from earlier installs is ignored on purpose — it defaulted to 50)
    max_attempts: int = int(os.getenv("PURCHASE_ATTEMPT_LIMIT", "0"))
    # Fast mode (ports / reserved IPs): max IP checks per job (0 = no limit; the old MAX_PORT_ATTEMPTS
    # is ignored on purpose) and how many at once.
    max_port_attempts: int = int(os.getenv("IP_HUNT_LIMIT", "0"))
    port_parallel: int = int(os.getenv("PORT_PARALLEL", "10"))
    # "Only working" purchase: the page the bot opens from each new server over SSH
    check_url: str = os.getenv("CHECK_URL", "https://turkmenistanairlines.tm/")
    check_ssh_timeout: int = int(os.getenv("CHECK_SSH_TIMEOUT", "300"))
    # Selectel IP scouting: max servers per run and how many exist at once (1 = strictly one by one).
    scan_max: int = int(os.getenv("SCAN_MAX", "200"))
    scan_parallel: int = int(os.getenv("SCAN_PARALLEL", "1"))
    # How many servers are created at the same time.
    parallel: int = int(os.getenv("PARALLEL", "3"))
    # Seconds to wait for a server to become ACTIVE.
    active_timeout: int = int(os.getenv("ACTIVE_TIMEOUT", "300"))
    # Boot volume size (GB) used when a flavor has no local disk.
    boot_volume_gb: int = int(os.getenv("BOOT_VOLUME_GB", "10"))
    # Manual location names, e.g. "eu-west1=NL:Amsterdam,us-west=US:Silicon Valley".
    region_names: str = os.getenv("REGION_NAMES", "")


config = Config()
