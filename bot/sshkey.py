"""The bot's own SSH key: added to every server bought in "only working" mode so the bot can log in
and check it. Created once, stored next to the database (private part never leaves the server)."""
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric import ed25519

from .config import config


def key_path() -> Path:
    return Path(config.db_path).parent / "bot_ssh_key"


def bot_key() -> tuple[str, str]:
    """(path to the private key, public key line). Generated on first use."""
    path = key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = ed25519.Ed25519PrivateKey.generate()
        path.write_bytes(key.private_bytes(S.Encoding.PEM, S.PrivateFormat.OpenSSH, S.NoEncryption()))
        os.chmod(path, 0o600)
        pub = key.public_key().public_bytes(S.Encoding.OpenSSH, S.PublicFormat.OpenSSH).decode()
        path.with_suffix(".pub").write_text(pub + " vps-buyer-bot\n")
    return str(path), path.with_suffix(".pub").read_text().strip()
