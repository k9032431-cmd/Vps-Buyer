import os
from dataclasses import dataclass

import aiosqlite
from cryptography.fernet import Fernet

from .config import config

_fernet = Fernet(config.encryption_key.encode()) if config.encryption_key else None


def _enc(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if _fernet else value


def _dec(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode() if _fernet else value


@dataclass
class Account:
    id: int
    user_id: int
    provider: str
    username: str
    password: str
    project_name: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    project_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    region TEXT NOT NULL,
    server_id TEXT NOT NULL,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    admin_pass TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS region_geo (
    provider TEXT NOT NULL,
    region TEXT NOT NULL,
    country TEXT NOT NULL,
    city TEXT NOT NULL,
    PRIMARY KEY (provider, region)
);
"""


async def init_db() -> None:
    os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
    async with aiosqlite.connect(config.db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def add_account(user_id: int, provider: str, username: str, password: str, project_name: str) -> int:
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO accounts (user_id, provider, username, password, project_name) VALUES (?,?,?,?,?)",
            (user_id, provider, username, _enc(password), project_name),
        )
        await db.commit()
        return cur.lastrowid


async def list_accounts(user_id: int, provider: str) -> list[Account]:
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, user_id, provider, username, password, project_name FROM accounts "
            "WHERE user_id=? AND provider=? ORDER BY id", (user_id, provider),
        )).fetchall()
    return [Account(r[0], r[1], r[2], r[3], _dec(r[4]), r[5]) for r in rows]


async def get_account(user_id: int, account_id: int) -> Account | None:
    async with aiosqlite.connect(config.db_path) as db:
        r = await (await db.execute(
            "SELECT id, user_id, provider, username, password, project_name FROM accounts WHERE id=? AND user_id=?",
            (account_id, user_id),
        )).fetchone()
    return Account(r[0], r[1], r[2], r[3], _dec(r[4]), r[5]) if r else None


async def delete_account(user_id: int, account_id: int) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user_id))
        await db.commit()


async def add_server(user_id: int, account_id: int, region: str, server_id: str, name: str, ip: str,
                     admin_pass: str | None) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO servers (user_id, account_id, region, server_id, name, ip, admin_pass) VALUES (?,?,?,?,?,?,?)",
            (user_id, account_id, region, server_id, name, ip, _enc(admin_pass) if admin_pass else None),
        )
        await db.commit()


async def count_servers(user_id: int) -> int:
    async with aiosqlite.connect(config.db_path) as db:
        r = await (await db.execute("SELECT COUNT(*) FROM servers WHERE user_id=?", (user_id,))).fetchone()
    return r[0]


async def count_accounts(user_id: int) -> int:
    async with aiosqlite.connect(config.db_path) as db:
        r = await (await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (user_id,))).fetchone()
    return r[0]


async def list_servers(user_id: int, limit: int = 30) -> list[tuple[str, str, str, str]]:
    async with aiosqlite.connect(config.db_path) as db:
        return await (await db.execute(
            "SELECT region, name, ip, created_at FROM servers WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )).fetchall()


async def get_region_geo(provider: str, region: str) -> tuple[str, str] | None:
    async with aiosqlite.connect(config.db_path) as db:
        r = await (await db.execute(
            "SELECT country, city FROM region_geo WHERE provider=? AND region=?", (provider, region))).fetchone()
    return (r[0], r[1]) if r else None


async def set_region_geo(provider: str, region: str, country: str, city: str) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR REPLACE INTO region_geo (provider, region, country, city) VALUES (?,?,?,?)",
                         (provider, region, country, city))
        await db.commit()
