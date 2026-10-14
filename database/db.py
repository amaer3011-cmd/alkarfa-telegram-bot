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
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiosqlite

from config import DB_URL, TIMEZONE

_write_lock: Optional[asyncio.Lock] = None

# In-memory caches (loaded by init_db) so the guard handler that runs on
# every update never has to touch the database to check ban / maintenance.
# _banned / _muted map user_id -> unix time the penalty ends (0 = forever).
_banned: dict = {}
_muted: dict = {}
# user_id -> (frozenset of restriction flags, daily file limit)
_restrict: dict = {}
# user_id -> owner_id of an open owner<->user conversation
_threads: dict = {}
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

_CREATE_ACTIVITY_TABLE = """
CREATE TABLE IF NOT EXISTS activity_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
);
"""
_CREATE_ACTIVITY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log (user_id, id);"
)

_CREATE_FILES_TABLE = """
CREATE TABLE IF NOT EXISTS files_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    orig_name   TEXT,
    new_name    TEXT,
    media_type  TEXT,
    size        INTEGER,
    file_id     TEXT,
    notes       TEXT
);
"""
_CREATE_FILES_INDEX = "CREATE INDEX IF NOT EXISTS idx_files_user ON files_log (user_id, id);"

_CREATE_THREADS_TABLE = """
CREATE TABLE IF NOT EXISTS chat_threads (
    user_id   INTEGER PRIMARY KEY,
    owner_id  INTEGER NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1,
    last_ts   INTEGER NOT NULL
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
    ("users", "banned_until", "INTEGER"),
    ("users", "muted_until", "INTEGER"),
    ("users", "restrict_flags", "TEXT"),
    ("users", "daily_limit", "INTEGER NOT NULL DEFAULT 0"),
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
        await db.execute(_CREATE_ACTIVITY_TABLE)
        await db.execute(_CREATE_ACTIVITY_INDEX)
        await db.execute(_CREATE_FILES_TABLE)
        await db.execute(_CREATE_FILES_INDEX)
        await db.execute(_CREATE_THREADS_TABLE)
        for table, column, col_type in _MIGRATIONS:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
            except aiosqlite.OperationalError:
                pass  # column already exists
        await db.commit()

        # Warm the in-memory caches used by the guard on every update.
        now = int(time.time())
        _banned.clear()
        async with db.execute(
            "SELECT user_id, banned_until FROM users WHERE is_banned = 1;"
        ) as cur:
            async for uid, until in cur:
                if not until or until > now:
                    _banned[uid] = until or 0
        _muted.clear()
        async with db.execute(
            "SELECT user_id, muted_until FROM users WHERE muted_until > ?;", (now,)
        ) as cur:
            async for uid, until in cur:
                _muted[uid] = until
        _restrict.clear()
        async with db.execute(
            "SELECT user_id, restrict_flags, daily_limit FROM users "
            "WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0;"
        ) as cur:
            async for uid, flags, limit in cur:
                _restrict[uid] = (_parse_flags(flags), int(limit or 0))
        _threads.clear()
        async with db.execute(
            "SELECT user_id, owner_id FROM chat_threads WHERE active = 1;"
        ) as cur:
            async for uid, owner in cur:
                _threads[uid] = owner
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
# Admin-panel helpers: profiles, bans, mutes, restrictions, statistics
# --------------------------------------------------------------------- #
RESTRICT_FLAGS = ("files", "video", "pdf", "settings")


def _parse_flags(raw: Optional[str]) -> frozenset:
    return frozenset(f for f in (raw or "").split(",") if f in RESTRICT_FLAGS)


@dataclass
class UserProfile:
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    date_joined: int
    last_seen: Optional[int]
    files_count: int
    is_banned: bool  # effective: a timed ban that already ended counts as False
    banned_until: Optional[int] = None  # None => permanent (when is_banned)
    muted_until: Optional[int] = None
    restrict_flags: frozenset = field(default_factory=frozenset)
    daily_limit: int = 0

    @property
    def is_muted(self) -> bool:
        return bool(self.muted_until and self.muted_until > time.time())

    @property
    def is_restricted(self) -> bool:
        return bool(self.restrict_flags) or self.daily_limit > 0


_PROFILE_COLUMNS = (
    "user_id, username, first_name, date_joined, last_seen, files_count, is_banned, "
    "banned_until, muted_until, restrict_flags, daily_limit"
)


def _profile(row) -> UserProfile:
    data = dict(row)
    now = time.time()
    banned = bool(data["is_banned"]) and (not data["banned_until"] or data["banned_until"] > now)
    data["is_banned"] = banned
    data["banned_until"] = (data["banned_until"] or None) if banned else None
    data["files_count"] = data["files_count"] or 0
    data["restrict_flags"] = _parse_flags(data["restrict_flags"])
    data["daily_limit"] = data["daily_limit"] or 0
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


def _active(cache: dict, user_id: int) -> bool:
    until = cache.get(user_id)
    if until is None:
        return False
    if until == 0 or until > time.time():
        return True
    cache.pop(user_id, None)  # the timed penalty ended
    return False


def is_banned(user_id: int) -> bool:
    return _active(_banned, user_id)


def is_muted(user_id: int) -> bool:
    return _active(_muted, user_id)


def get_restrictions(user_id: int) -> tuple:
    """(frozenset of flags, daily file limit). Cheap, in-memory."""
    return _restrict.get(user_id, (frozenset(), 0))


async def set_banned(user_id: int, banned: bool, until: Optional[int] = None) -> None:
    """Ban (until = unix time, None = forever) or unban a user."""
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, date_joined, is_banned, banned_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_banned = excluded.is_banned, banned_until = excluded.banned_until;
                """,
                (user_id, int(time.time()), 1 if banned else 0, until if banned else None),
            )
            await db.commit()
    if banned:
        _banned[user_id] = int(until or 0)
    else:
        _banned.pop(user_id, None)


async def set_muted(user_id: int, until: Optional[int]) -> None:
    """Mute (bot ignores the user completely) until `until`; None = unmute.
    Use until=0 for a mute with no end."""
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, date_joined, muted_until) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET muted_until = excluded.muted_until;
                """,
                (user_id, int(time.time()), None if until is None else until or 4102444800),
            )
            await db.commit()
    if until is None:
        _muted.pop(user_id, None)
    else:
        _muted[user_id] = int(until)


async def set_restrictions(user_id: int, flags, daily_limit: int) -> None:
    flags = frozenset(f for f in flags if f in RESTRICT_FLAGS)
    daily_limit = max(0, int(daily_limit))
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, date_joined, restrict_flags, daily_limit)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    restrict_flags = excluded.restrict_flags,
                    daily_limit = excluded.daily_limit;
                """,
                (user_id, int(time.time()), ",".join(sorted(flags)), daily_limit),
            )
            await db.commit()
    if flags or daily_limit:
        _restrict[user_id] = (flags, daily_limit)
    else:
        _restrict.pop(user_id, None)


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
    """Look a user up by numeric id or by exact @username."""
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


async def search_profiles(query: str, limit: int = 10) -> list:
    """Id / @username / part of the name or username."""
    query = query.strip()
    if not query:
        return []
    if query.lstrip("-").isdigit():
        p = await get_profile(int(query))
        return [p] if p else []
    like = f"%{query.lstrip('@')}%"
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users "
            "WHERE username LIKE ? COLLATE NOCASE OR first_name LIKE ? COLLATE NOCASE "
            "ORDER BY COALESCE(last_seen, 0) DESC LIMIT ?;",
            (like, like, limit),
        )
        return [_profile(r) for r in await cur.fetchall()]


# user-list filters: a=all (recent activity), n=newest, b=banned, m=muted,
# r=restricted, o=active in the last 24h
USER_FILTERS = ("a", "n", "b", "m", "r", "o")


def _filter_sql(flt: str) -> tuple:
    now = int(time.time())
    if flt == "b":
        return (
            "WHERE is_banned = 1 AND (COALESCE(banned_until, 0) = 0 OR banned_until > ?)",
            (now,), "COALESCE(last_seen, 0) DESC",
        )
    if flt == "m":
        return "WHERE muted_until > ?", (now,), "muted_until ASC"
    if flt == "r":
        return (
            "WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0",
            (), "COALESCE(last_seen, 0) DESC",
        )
    if flt == "o":
        return "WHERE last_seen >= ?", (now - 86400,), "last_seen DESC"
    if flt == "n":
        return "", (), "date_joined DESC, user_id DESC"
    return "", (), "COALESCE(last_seen, 0) DESC, user_id DESC"


async def count_profiles(flt: str = "a") -> int:
    where, params, _ = _filter_sql(flt)
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM users {where};", params)
        return int((await cur.fetchone())[0] or 0)


async def list_profiles(offset: int = 0, limit: int = 10, flt: str = "a") -> list:
    where, params, order = _filter_sql(flt)
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM users {where} ORDER BY {order} LIMIT ? OFFSET ?;",
            (*params, limit, offset),
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
    """Every user who is not banned right now."""
    now = int(time.time())
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(
            "SELECT user_id FROM users WHERE is_banned = 0 "
            "OR (COALESCE(banned_until, 0) != 0 AND banned_until <= ?);",
            (now,),
        )
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
            "banned": await one(
                "SELECT COUNT(*) FROM users WHERE is_banned = 1 "
                "AND (COALESCE(banned_until, 0) = 0 OR banned_until > ?);", (now,)
            ),
            "muted": await one("SELECT COUNT(*) FROM users WHERE muted_until > ?;", (now,)),
            "restricted": await one(
                "SELECT COUNT(*) FROM users WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0;"
            ),
            "files": await one("SELECT SUM(files_count) FROM users;"),
            "channels": await one("SELECT COUNT(*) FROM channels WHERE is_active = 1;"),
        }


# --------------------------------------------------------------------- #
# Activity log (everything a user did with the bot / was done to him)
# --------------------------------------------------------------------- #
_KEEP_EVENTS = 3000  # per user


async def log_event(user_id: int, kind: str, detail: Optional[str] = None) -> None:
    """Never raises: logging must not break the bot."""
    try:
        async with _lock():
            async with aiosqlite.connect(DB_URL) as db:
                await db.execute(
                    "INSERT INTO activity_log (user_id, ts, kind, detail) VALUES (?, ?, ?, ?);",
                    (user_id, int(time.time()), kind, (detail or "")[:500]),
                )
                if random.random() < 0.01:  # occasional trim
                    await db.execute(
                        "DELETE FROM activity_log WHERE user_id = ? AND id NOT IN "
                        "(SELECT id FROM activity_log WHERE user_id = ? ORDER BY id DESC LIMIT ?);",
                        (user_id, user_id, _KEEP_EVENTS),
                    )
                await db.commit()
    except Exception:
        pass


def _kinds_sql(kinds) -> tuple:
    if not kinds:
        return "", ()
    return f" AND kind IN ({','.join('?' for _ in kinds)})", tuple(kinds)


async def count_events(user_id: int, kinds=None) -> int:
    extra, params = _kinds_sql(kinds)
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) FROM activity_log WHERE user_id = ?{extra};", (user_id, *params)
        )
        return int((await cur.fetchone())[0] or 0)


async def get_events(user_id: int, kinds=None, offset: int = 0, limit: int = 12) -> list:
    """Newest first. Rows are (ts, kind, detail)."""
    extra, params = _kinds_sql(kinds)
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(
            f"SELECT ts, kind, detail FROM activity_log WHERE user_id = ?{extra} "
            "ORDER BY id DESC LIMIT ? OFFSET ?;",
            (user_id, *params, limit, offset),
        )
        return await cur.fetchall()


# --------------------------------------------------------------------- #
# Files the bot edited for users
# --------------------------------------------------------------------- #
@dataclass
class FileRecord:
    id: int
    user_id: int
    ts: int
    orig_name: Optional[str]
    new_name: Optional[str]
    media_type: Optional[str]
    size: Optional[int]
    file_id: Optional[str]
    notes: Optional[str]
    first_name: Optional[str] = None
    username: Optional[str] = None


_FILE_SELECT = (
    "SELECT f.id, f.user_id, f.ts, f.orig_name, f.new_name, f.media_type, f.size, "
    "f.file_id, f.notes, u.first_name, u.username "
    "FROM files_log f LEFT JOIN users u ON u.user_id = f.user_id"
)


async def log_file(
    user_id: int, orig_name: Optional[str], new_name: Optional[str], media_type: str,
    size: Optional[int], file_id: Optional[str], notes: Optional[str] = None,
) -> int:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            cur = await db.execute(
                "INSERT INTO files_log (user_id, ts, orig_name, new_name, media_type, size, "
                "file_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (user_id, int(time.time()), orig_name, new_name, media_type, size, file_id, notes),
            )
            await db.commit()
            return cur.lastrowid


async def get_file(file_row_id: int) -> Optional[FileRecord]:
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(f"{_FILE_SELECT} WHERE f.id = ?;", (file_row_id,))
        row = await cur.fetchone()
        return FileRecord(*row) if row else None


async def count_files(user_id: Optional[int] = None) -> int:
    async with aiosqlite.connect(DB_URL) as db:
        if user_id is None:
            cur = await db.execute("SELECT COUNT(*) FROM files_log;")
        else:
            cur = await db.execute("SELECT COUNT(*) FROM files_log WHERE user_id = ?;", (user_id,))
        return int((await cur.fetchone())[0] or 0)


async def list_files(user_id: Optional[int] = None, offset: int = 0, limit: int = 8) -> list:
    async with aiosqlite.connect(DB_URL) as db:
        if user_id is None:
            cur = await db.execute(f"{_FILE_SELECT} ORDER BY f.id DESC LIMIT ? OFFSET ?;", (limit, offset))
        else:
            cur = await db.execute(
                f"{_FILE_SELECT} WHERE f.user_id = ? ORDER BY f.id DESC LIMIT ? OFFSET ?;",
                (user_id, limit, offset),
            )
        return [FileRecord(*r) for r in await cur.fetchall()]


def _day_start() -> int:
    """Unix time of today's midnight in the configured time zone."""
    from datetime import datetime, timezone

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(TIMEZONE)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def files_today(user_id: int) -> int:
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM files_log WHERE user_id = ? AND ts >= ?;",
            (user_id, _day_start()),
        )
        return int((await cur.fetchone())[0] or 0)


async def file_stats(user_id: int) -> tuple:
    """(count, total bytes, last timestamp)"""
    async with aiosqlite.connect(DB_URL) as db:
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(ts) FROM files_log WHERE user_id = ?;",
            (user_id,),
        )
        row = await cur.fetchone()
        return int(row[0] or 0), int(row[1] or 0), row[2]


# --------------------------------------------------------------------- #
# Owner <-> user conversations
# --------------------------------------------------------------------- #
CHAT_IDLE_SECONDS = 72 * 3600  # a thread nobody writes in for this long closes itself


def thread_owner(user_id: int) -> Optional[int]:
    """Owner id if this user has an open conversation, else None. In-memory."""
    return _threads.get(user_id)


async def open_thread(user_id: int, owner_id: int) -> None:
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute(
                """
                INSERT INTO chat_threads (user_id, owner_id, active, last_ts) VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    owner_id = excluded.owner_id, active = 1, last_ts = excluded.last_ts;
                """,
                (user_id, owner_id, int(time.time())),
            )
            await db.commit()
    _threads[user_id] = owner_id
    _thread_ts[user_id] = time.time()


_thread_ts: dict = {}


def thread_alive(user_id: int) -> bool:
    """False (and forgets the thread) once it has been idle too long."""
    if user_id not in _threads:
        return False
    if time.time() - _thread_ts.get(user_id, time.time()) > CHAT_IDLE_SECONDS:
        _threads.pop(user_id, None)
        return False
    _thread_ts.setdefault(user_id, time.time())
    return True


def touch_thread(user_id: int) -> None:
    if user_id in _threads:
        _thread_ts[user_id] = time.time()


async def close_thread(user_id: int) -> None:
    _threads.pop(user_id, None)
    _thread_ts.pop(user_id, None)
    async with _lock():
        async with aiosqlite.connect(DB_URL) as db:
            await db.execute("UPDATE chat_threads SET active = 0 WHERE user_id = ?;", (user_id,))
            await db.commit()


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
