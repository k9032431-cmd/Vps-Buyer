"""Self-update: git pull + pip install, then the bot restarts itself."""
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import config

ROOT = Path(__file__).resolve().parent.parent
NOTIFY_FILE = Path(config.db_path).parent / "update_notify.json"


class UpdateError(Exception):
    pass


@dataclass
class UpdateResult:
    updated: bool
    old: str
    new: str
    changes: list[str]


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 300)
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise UpdateError(f"{' '.join(cmd[:3])}: {text[-800:]}")
    return text


async def current_version() -> str:
    try:
        return await _run("git", "log", "-1", "--format=%h (%cd)", "--date=format:%d.%m.%Y %H:%M")
    except Exception:
        return "неизвестно"


async def update() -> UpdateResult:
    if not (ROOT / ".git").exists():
        raise UpdateError("Бот установлен не через git — переустановите через install.sh")
    old = await _run("git", "rev-parse", "--short", "HEAD")
    await _run("git", "pull", "--ff-only", "-q")
    new = await _run("git", "rev-parse", "--short", "HEAD")
    if old == new:
        return UpdateResult(False, old, new, [])
    changes = (await _run("git", "log", "--format=%s", f"{old}..{new}")).splitlines()
    await _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")
    return UpdateResult(True, old, new, changes)


def save_notify(chat_id: int, text: str) -> None:
    NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_FILE.write_text(json.dumps({"chat_id": chat_id, "text": text}))


def pop_notify() -> dict | None:
    try:
        data = json.loads(NOTIFY_FILE.read_text())
        NOTIFY_FILE.unlink()
        return data
    except Exception:
        return None


def restart_process() -> None:
    """Replace the current process with a fresh one (same PID, so systemd keeps tracking it)."""
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-m", "bot.main"])
