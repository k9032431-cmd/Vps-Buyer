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
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    # Fernet key used to encrypt stored provider passwords.
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    hostvds_auth_url: str = os.getenv("HOSTVDS_AUTH_URL", "https://os-api.hostvds.com/identity")
    selectel_api_url: str = os.getenv("SELECTEL_API_URL", "https://api.vscale.io/v1")
    # Safety limit: max servers created in a single purchase job (incl. deleted ones).
    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "50"))
    # How many servers are created at the same time.
    parallel: int = int(os.getenv("PARALLEL", "3"))
    # Seconds to wait for a server to become ACTIVE.
    active_timeout: int = int(os.getenv("ACTIVE_TIMEOUT", "300"))
    # Boot volume size (GB) used when a flavor has no local disk.
    boot_volume_gb: int = int(os.getenv("BOOT_VOLUME_GB", "10"))
    # Manual location names, e.g. "eu-west1=NL:Amsterdam,us-west=US:Silicon Valley".
    region_names: str = os.getenv("REGION_NAMES", "")


config = Config()
