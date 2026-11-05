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
CREATE TABLE IF NOT EXISTS ip_seen (
    provider TEXT NOT NULL,
    region TEXT NOT NULL,
    ip TEXT NOT NULL,
    PRIMARY KEY (provider, region, ip)
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


@dataclass
class ServerRow:
    id: int
    account_id: int
    region: str
    server_id: str
    name: str
    ip: str
    admin_pass: str | None
    created_at: str


_SERVER_COLS = "id, account_id, region, server_id, name, ip, admin_pass, created_at"


def _server(r) -> ServerRow:
    return ServerRow(r[0], r[1], r[2], r[3], r[4], r[5], _dec(r[6]) if r[6] else None, r[7])


async def list_servers(user_id: int, limit: int = 50) -> list[ServerRow]:
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            f"SELECT {_SERVER_COLS} FROM servers WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit),
        )).fetchall()
    return [_server(r) for r in rows]


async def get_server(user_id: int, row_id: int) -> ServerRow | None:
    async with aiosqlite.connect(config.db_path) as db:
        r = await (await db.execute(
            f"SELECT {_SERVER_COLS} FROM servers WHERE id=? AND user_id=?", (row_id, user_id))).fetchone()
    return _server(r) if r else None


async def delete_server_row(user_id: int, row_id: int) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("DELETE FROM servers WHERE id=? AND user_id=?", (row_id, user_id))
        await db.commit()


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


async def add_seen_ips(provider: str, region: str, ips: list[str]) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.executemany("INSERT OR IGNORE INTO ip_seen (provider, region, ip) VALUES (?,?,?)",
                             [(provider, region, ip) for ip in ips])
        await db.commit()


async def seen_ips(provider: str, region: str) -> list[str]:
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT ip FROM ip_seen WHERE provider=? AND region=?", (provider, region))).fetchall()
    return [r[0] for r in rows]
