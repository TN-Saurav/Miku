from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Track


def normalize_query(query: str) -> str:
    """Normalize equivalent text without fuzzy-matching unrelated queries."""
    text = unicodedata.normalize("NFKC", query).casefold().strip()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version(version)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS playback_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                normalized_query TEXT NOT NULL,
                track_json TEXT NOT NULL,
                played_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER NOT NULL,
                config_key TEXT NOT NULL,
                config_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, config_key)
            );
            """
        )
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.initialize() must be awaited first")
        return self._conn

    async def mark_played(self, guild_id: int, user_id: int, query: str, track: Track) -> None:
        normalized = normalize_query(query)
        played_at = utc_now()
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                "INSERT INTO playback_history(guild_id,user_id,normalized_query,track_json,played_at) "
                "VALUES(?,?,?,?,?)",
                (guild_id, user_id, normalized, json.dumps(track.to_dict(), ensure_ascii=False), played_at),
            )
            await conn.commit()

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        async with self._lock:
            await self._connection().execute(
                """INSERT INTO guild_config(guild_id,config_key,config_value,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(guild_id,config_key) DO UPDATE SET
                config_value=excluded.config_value,updated_at=excluded.updated_at""",
                (guild_id, key, value, utc_now()),
            )
            await self._connection().commit()

    async def get_guild_config(self, guild_id: int, key: str) -> str | None:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT config_value FROM guild_config WHERE guild_id=? AND config_key=?",
                (guild_id, key),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row["config_value"] if row else None
