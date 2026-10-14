"""
database/db.py
===============
Thin async data-access layer built on top of `aiosqlite`.

Design notes
------------
* Every call opens a short-lived `aiosqlite.connect()` context (SQLite
  handles many short connections well and it avoids sharing one cursor
  between concurrent asyncio tasks).
* Writes are upserts and are serialised with a module-level lock to avoid
  "database is locked" errors.
* New columns are added with defensive `ALTER TABLE` migrations, so an
  existing `bot_database.db` upgrades in place.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

from config import DB_URL

_write_lock: Optional[asyncio.Lock] = None

# In-memory caches (loaded by init_db) so the guard handler that runs on
# every update never has to touch the database to check ban / maintenance.
_banned: set = set()
_settings: dict = {}


def _lock() -> asyncio.Lock:
    # Created lazily so it binds to the running event loop (Python < 3.10).
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    return _write_lock


# --------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------- #
@dataclass
class UserSettings:
    user_id: int
    thumbnail_path: Optional[str]
    custom_caption: Optional[str]
    watermark_patterns: Optional[str]
    date_joined: int
    cover_path: Optional[str] = None
    cover_enabled: bool = True
    cover_mode: str = "insert"


@dataclass
class ChannelSettings:
    channel_id: int
    default_thumbnail_path: Optional[str]
    channel_caption_template: Optional[str]
    channel_watermark_patterns: Optional[str]
    bound_by: Optional[int]
    is_active: bool
    channel_cover_path: Optional[str] = None


_USER_COLUMNS = (
    "user_id, thumbnail_path, custom_caption, watermark_patterns, date_joined, "
    "cover_path, cover_enabled, cover_mode"
)
_CHANNEL_COLUMNS = (
    "channel_id, default_thumbnail_path, channel_caption_template, "
    "channel_watermark_patterns, bound_by, is_active, channel_cover_path"
)

# --------------------------------------------------------------------- #
# Schema setup
# --------------------------------------------------------------------- #
_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id             INTEGER PRIMARY KEY,
    thumbnail_path      TEXT,
    custom_caption      TEXT,
    watermark_patterns  TEXT,
    date_joined         INTEGER NOT NULL,
    cover_path          TEXT,
    cover_enabled       INTEGER NOT NULL DEFAULT 1,
    cover_mode          TEXT NOT NULL DEFAULT 'insert'
);
"""

_CREATE_CHANNELS_TABLE = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id                  INTEGER PRIMARY KEY,
    default_thumbnail_path      TEXT,
    channel_caption_template    TEXT,
    channel_watermark_patterns  TEXT,
    bound_by                    INTEGER,
    is_active                   INTEGER NOT NULL DEFAULT 1,
    channel_cover_path          TEXT
);
"""

_CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS bot_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

_CREATE_VIDEO_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS video_settings (
    user_id         INTEGER PRIMARY KEY,
    wm_enabled      INTEGER NOT NULL DEFAULT 0,
    wm_mode         TEXT NOT NULL DEFAULT 'text',
    wm_text         TEXT,
    wm_image_path   TEXT,
    wm_position     TEXT NOT NULL DEFAULT 'br',
    wm_size         INTEGER NOT NULL DEFAULT 20,
    wm_opacity      INTEGER NOT NULL DEFAULT 70,
    title_template  TEXT,
    title_overlay   INTEGER NOT NULL DEFAULT 0
);
"""

_MIGRATIONS = (
    ("users", "username", "TEXT"),
    ("users", "first_name", "TEXT"),
    ("users", "last_seen", "INTEGER"),
    ("users", "files_count", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "is_banned", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "watermark_patterns", "TEXT"),
    ("users", "cover_path", "TEXT"),
    ("users", "cover_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "cover_mode", "TEXT NOT NULL DEFAULT 'insert'"),
    ("channels", "channel_watermark_patterns", "TEXT"),
    ("channels", "channel_cover_path", "TEXT"),
)

_USER_FIELDS = {
    "thumbnail_path", "custom_caption", "watermark_patterns",
    "cover_path", "cover_enabled", "cover_mode",
}
_CHANNEL_FIELDS = {
    "default_thumbnail_path", "channel_caption_template",
    "channel_watermark_patterns", "channel_cover_path",
}


async def init_db() -> None:
    """Create tables if they do not already exist. Call once at startup."""
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(_CREATE_USERS_TABLE)
        await db.execute(_CREATE_CHANNELS_TABLE)
        await db.execute(_CREATE_SETTINGS_TABLE)
        await db.execute(_CREATE_VIDEO_SETTINGS_TABLE)
        for table, column, col_type in _MIGRATIONS:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
            except aiosqlite.OperationalError:
                pass  # column already exists
        await db.commit()

        # Warm the in-memory caches used by the guard on every update.
        _banned.clear()
        async with db.execute("SELECT user_id FROM users WHERE is_banned = 1;") as cur:
            async for (uid,) in cur:
                _banned.add(uid)
        _settings.clear()
        async with db.execute("SELECT key, value FROM bot_settings;") as cur:
            async for key, value in cur:
                _settings[key] = value


# --------------------------------------------------------------------- #
# Internal upsert helpers (field names are whitelisted)
# --------------------------------------------------------------------- #
async def _set_user_field(user_id: int, field: str, value: Any) -> None:
    assert field in _USER_FIELDS, field
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                f"""
                INSERT INTO users (user_id, {field}, date_joined) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET {field} = excluded.{field};
                """,
                (user_id, value, int(time.time())),
            )
            await db.commit()


async def _set_channel_field(channel_id: int, field: str, value: Any) -> None:
    assert field in _CHANNEL_FIELDS, field
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                f"""
                INSERT INTO channels (channel_id, {field}, is_active) VALUES (?, ?, 1)
                ON CONFLICT(channel_id) DO UPDATE SET {field} = excluded.{field};
                """,
                (channel_id, value),
            )
            await db.commit()


# --------------------------------------------------------------------- #
# User operations
# --------------------------------------------------------------------- #
async def ensure_user(user_id: int) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                "INSERT INTO users (user_id, date_joined) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO NOTHING;",
                (user_id, int(time.time())),
            )
            await db.commit()


async def get_user(user_id: int) -> Optional[UserSettings]:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE user_id = ?;", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["cover_enabled"] = bool(data["cover_enabled"])
        return UserSettings(**data)


async def set_user_thumbnail(user_id: int, path: str) -> None:
    await _set_user_field(user_id, "thumbnail_path", path)


async def delete_user_thumbnail(user_id: int) -> None:
    await _set_user_field(user_id, "thumbnail_path", None)


async def set_user_caption(user_id: int, caption: str) -> None:
    await _set_user_field(user_id, "custom_caption", caption)


async def delete_user_caption(user_id: int) -> None:
    await _set_user_field(user_id, "custom_caption", None)


async def set_user_watermark_patterns(user_id: int, patterns: str) -> None:
    await _set_user_field(user_id, "watermark_patterns", patterns)


async def delete_user_watermark_patterns(user_id: int) -> None:
    await _set_user_field(user_id, "watermark_patterns", None)


async def set_user_cover(user_id: int, path: str) -> None:
    await _set_user_field(user_id, "cover_path", path)


async def delete_user_cover(user_id: int) -> None:
    await _set_user_field(user_id, "cover_path", None)


async def set_user_cover_enabled(user_id: int, enabled: bool) -> None:
    await _set_user_field(user_id, "cover_enabled", 1 if enabled else 0)


async def set_user_cover_mode(user_id: int, mode: str) -> None:
    await _set_user_field(user_id, "cover_mode", mode)


# --------------------------------------------------------------------- #
# Channel operations
# --------------------------------------------------------------------- #
async def bind_channel(channel_id: int, bound_by: int) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO channels (channel_id, bound_by, is_active) VALUES (?, ?, 1)
                ON CONFLICT(channel_id) DO UPDATE SET is_active = 1, bound_by = excluded.bound_by;
                """,
                (channel_id, bound_by),
            )
            await db.commit()


async def unbind_channel(channel_id: int) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                "UPDATE channels SET is_active = 0 WHERE channel_id = ?;", (channel_id,)
            )
            await db.commit()


async def get_channel(channel_id: int) -> Optional[ChannelSettings]:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT {_CHANNEL_COLUMNS} FROM channels WHERE channel_id = ?;", (channel_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["is_active"] = bool(data["is_active"])
        return ChannelSettings(**data)


async def set_channel_thumbnail(channel_id: int, path: str) -> None:
    await _set_channel_field(channel_id, "default_thumbnail_path", path)


async def set_channel_caption(channel_id: int, caption: str) -> None:
    await _set_channel_field(channel_id, "channel_caption_template", caption)


async def set_channel_watermark_patterns(channel_id: int, patterns: str) -> None:
    await _set_channel_field(channel_id, "channel_watermark_patterns", patterns)


async def delete_channel_watermark_patterns(channel_id: int) -> None:
    await _set_channel_field(channel_id, "channel_watermark_patterns", None)


async def set_channel_cover(channel_id: int, path: str) -> None:
    await _set_channel_field(channel_id, "channel_cover_path", path)


async def delete_channel_cover(channel_id: int) -> None:
    await _set_channel_field(channel_id, "channel_cover_path", None)


# --------------------------------------------------------------------- #
# Bot settings (key/value) — cached in memory
# --------------------------------------------------------------------- #
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    return _settings.get(key, default)


async def set_setting(key: str, value: Optional[str]) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            if value is None:
                await db.execute("DELETE FROM bot_settings WHERE key = ?;", (key,))
            else:
                await db.execute(
                    "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    (key, value),
                )
            await db.commit()
    if value is None:
        _settings.pop(key, None)
    else:
        _settings[key] = value


# --------------------------------------------------------------------- #
# Admin-panel helpers: profiles, bans, statistics
# --------------------------------------------------------------------- #
@dataclass
class UserProfile:
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    date_joined: int
    last_seen: Optional[int]
    files_count: int
    is_banned: bool


_PROFILE_COLUMNS = "user_id, username, first_name, date_joined, last_seen, files_count, is_banned"


def _profile(row) -> UserProfile:
    data = dict(row)
    data["is_banned"] = bool(data["is_banned"])
    data["files_count"] = data["files_count"] or 0
    return UserProfile(**data)


async def touch_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    """Register the user (if new) and refresh name / username / last seen."""
    now = int(time.time())
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, date_joined, username, first_name, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = excluded.last_seen;
                """,
                (user_id, now, username, first_name, now),
            )
            await db.commit()


def is_banned(user_id: int) -> bool:
    return user_id in _banned


async def set_banned(user_id: int, banned: bool) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, date_joined, is_banned) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET is_banned = excluded.is_banned;
                """,
                (user_id, int(time.time()), 1 if banned else 0),
            )
            await db.commit()
    if banned:
        _banned.add(user_id)
    else:
        _banned.discard(user_id)


async def incr_files(user_id: int) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                "UPDATE users SET files_count = COALESCE(files_count, 0) + 1 WHERE user_id = ?;",
                (user_id,),
            )
            await db.commit()


async def get_profile(user_id: int) -> Optional[UserProfile]:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users WHERE user_id = ?;", (user_id,)
        )
        row = await cur.fetchone()
        return _profile(row) if row else None


async def find_profile(query: str) -> Optional[UserProfile]:
    """Look a user up by numeric id or by @username."""
    query = query.strip()
    if query.lstrip("-").isdigit():
        return await get_profile(int(query))
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE;",
            (query.lstrip("@"),),
        )
        row = await cur.fetchone()
        return _profile(row) if row else None


async def list_profiles(offset: int = 0, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users "
            "ORDER BY date_joined DESC, user_id DESC LIMIT ? OFFSET ?;",
            (limit, offset),
        )
        return [_profile(r) for r in await cur.fetchall()]


async def all_profiles() -> list:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users ORDER BY date_joined ASC;"
        )
        return [_profile(r) for r in await cur.fetchall()]


async def broadcast_ids() -> list:
    """Every non-banned user id."""
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned = 0;")
        return [r[0] for r in await cur.fetchall()]


async def get_stats() -> dict:
    now = int(time.time())
    day, week = 24 * 3600, 7 * 24 * 3600
    async with aiosqlite.connect(DB_URL) as db:
        async def one(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return int(row[0] or 0)

        return {
            "total": await one("SELECT COUNT(*) FROM users;"),
            "new_24h": await one("SELECT COUNT(*) FROM users WHERE date_joined >= ?;", (now - day,)),
            "new_7d": await one("SELECT COUNT(*) FROM users WHERE date_joined >= ?;", (now - week,)),
            "active_24h": await one("SELECT COUNT(*) FROM users WHERE last_seen >= ?;", (now - day,)),
            "banned": await one("SELECT COUNT(*) FROM users WHERE is_banned = 1;"),
            "files": await one("SELECT SUM(files_count) FROM users;"),
            "channels": await one("SELECT COUNT(*) FROM channels WHERE is_active = 1;"),
        }


# --------------------------------------------------------------------- #
# Video settings (watermark / default title) — one row per user
# --------------------------------------------------------------------- #
@dataclass
class VideoSettings:
    user_id: int
    wm_enabled: bool = False
    wm_mode: str = "text"  # "text" | "image"
    wm_text: Optional[str] = None
    wm_image_path: Optional[str] = None
    wm_position: str = "br"
    wm_size: int = 20
    wm_opacity: int = 70
    title_template: Optional[str] = None
    title_overlay: bool = False


_VIDEO_FIELDS = {
    "wm_enabled", "wm_mode", "wm_text", "wm_image_path", "wm_position",
    "wm_size", "wm_opacity", "title_template", "title_overlay",
}
_VIDEO_COLUMNS = "user_id, " + ", ".join(sorted(_VIDEO_FIELDS))


async def get_video_settings(user_id: int) -> VideoSettings:
    """Always returns settings (defaults if the user never configured any)."""
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM video_settings WHERE user_id = ?;", (user_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return VideoSettings(user_id=user_id)
    data = dict(row)
    data["wm_enabled"] = bool(data["wm_enabled"])
    data["title_overlay"] = bool(data["title_overlay"])
    return VideoSettings(**data)


async def update_video_settings(user_id: int, **fields: Any) -> None:
    """Upsert one or more video-setting fields atomically."""
    assert fields and set(fields) <= _VIDEO_FIELDS, fields
    cols = list(fields)
    values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                f"INSERT INTO video_settings (user_id, {', '.join(cols)}) "
                f"VALUES (?, {placeholders}) "
                f"ON CONFLICT(user_id) DO UPDATE SET {updates};",
                (user_id, *values),
            )
            await db.commit()
