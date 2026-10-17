"""
database/db.py
===============
Async data-access layer on top of PostgreSQL (asyncpg).

Design notes
------------
* One connection pool for the whole bot; PostgreSQL handles concurrent
  writers itself, so no application-level write lock is needed.
* Connection string comes from the DATABASE_URL environment variable
  (Railway PostgreSQL plugin sets it automatically).
* Tables are created with CREATE TABLE IF NOT EXISTS and new columns are
  added with ADD COLUMN IF NOT EXISTS, so the schema upgrades in place.
* Small files the bot keeps on disk (thumbnails, covers, watermark logos)
  are mirrored into the `stored_files` table and restored at start-up, so
  they survive redeploys even without a Railway Volume.
"""

import asyncio
import glob
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

from config import DATABASE_URL, TIMEZONE

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

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


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database is not initialised: call init_db() first.")
    return _pool


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
    custom_link: Optional[str] = None  # makes the file name in the caption a link


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
    "cover_path, cover_enabled, cover_mode, custom_link"
)
_CHANNEL_COLUMNS = (
    "channel_id, default_thumbnail_path, channel_caption_template, "
    "channel_watermark_patterns, bound_by, is_active, channel_cover_path"
)

# --------------------------------------------------------------------- #
# Schema setup
# --------------------------------------------------------------------- #
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id             BIGINT PRIMARY KEY,
        thumbnail_path      TEXT,
        custom_caption      TEXT,
        watermark_patterns  TEXT,
        date_joined         BIGINT NOT NULL,
        cover_path          TEXT,
        cover_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
        cover_mode          TEXT NOT NULL DEFAULT 'insert',
        custom_link         TEXT,
        username            TEXT,
        first_name          TEXT,
        last_seen           BIGINT,
        files_count         BIGINT NOT NULL DEFAULT 0,
        is_banned           BOOLEAN NOT NULL DEFAULT FALSE,
        banned_until        BIGINT,
        muted_until         BIGINT,
        restrict_flags      TEXT,
        daily_limit         INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        channel_id                  BIGINT PRIMARY KEY,
        default_thumbnail_path      TEXT,
        channel_caption_template    TEXT,
        channel_watermark_patterns  TEXT,
        bound_by                    BIGINT,
        is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
        channel_cover_path          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_settings (
        key    TEXT PRIMARY KEY,
        value  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_settings (
        user_id         BIGINT PRIMARY KEY,
        wm_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
        wm_mode         TEXT NOT NULL DEFAULT 'text',
        wm_text         TEXT,
        wm_image_path   TEXT,
        wm_position     TEXT NOT NULL DEFAULT 'br',
        wm_size         INTEGER NOT NULL DEFAULT 20,
        wm_opacity      INTEGER NOT NULL DEFAULT 70,
        title_template  TEXT,
        title_overlay   BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id       BIGSERIAL PRIMARY KEY,
        user_id  BIGINT NOT NULL,
        ts       BIGINT NOT NULL,
        kind     TEXT NOT NULL,
        detail   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log (user_id, id)",
    """
    CREATE TABLE IF NOT EXISTS files_log (
        id          BIGSERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL,
        ts          BIGINT NOT NULL,
        orig_name   TEXT,
        new_name    TEXT,
        media_type  TEXT,
        size        BIGINT,
        file_id     TEXT,
        notes       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_user ON files_log (user_id, id)",
    """
    CREATE TABLE IF NOT EXISTS chat_threads (
        user_id   BIGINT PRIMARY KEY,
        owner_id  BIGINT NOT NULL,
        active    BOOLEAN NOT NULL DEFAULT TRUE,
        last_ts   BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stored_files (
        path        TEXT PRIMARY KEY,
        data        BYTEA NOT NULL,
        updated_at  BIGINT NOT NULL
    )
    """,
)

# Columns added after the first release (kept so an older database upgrades).
_MIGRATIONS = (
    ("users", "username", "TEXT"),
    ("users", "first_name", "TEXT"),
    ("users", "last_seen", "BIGINT"),
    ("users", "files_count", "BIGINT NOT NULL DEFAULT 0"),
    ("users", "is_banned", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "watermark_patterns", "TEXT"),
    ("users", "cover_path", "TEXT"),
    ("users", "cover_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("users", "cover_mode", "TEXT NOT NULL DEFAULT 'insert'"),
    ("users", "banned_until", "BIGINT"),
    ("users", "muted_until", "BIGINT"),
    ("users", "restrict_flags", "TEXT"),
    ("users", "daily_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "custom_link", "TEXT"),
    ("channels", "channel_watermark_patterns", "TEXT"),
    ("channels", "channel_cover_path", "TEXT"),
)

_USER_FIELDS = {
    "thumbnail_path", "custom_caption", "watermark_patterns",
    "cover_path", "cover_enabled", "cover_mode", "custom_link",
}
_CHANNEL_FIELDS = {
    "default_thumbnail_path", "channel_caption_template",
    "channel_watermark_patterns", "channel_cover_path",
}


async def init_db() -> None:
    """Connect, create tables if needed, restore saved files, warm caches."""
    global _pool
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Add a PostgreSQL database in Railway and "
            "reference its DATABASE_URL variable from the bot service."
        )
    last_exc: Optional[Exception] = None
    for attempt in range(1, 11):  # the database may still be starting on a fresh deploy
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=10, command_timeout=60
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Database not reachable yet (attempt %s/10): %s", attempt, exc)
            await asyncio.sleep(min(2 * attempt, 10))
    else:
        raise RuntimeError(f"Could not connect to PostgreSQL: {last_exc}")

    async with _pool.acquire() as con:
        async with con.transaction():
            # serialise concurrent start-ups (e.g. two replicas during a deploy)
            await con.execute("SELECT pg_advisory_xact_lock(727401)")
            for stmt in _SCHEMA:
                await con.execute(stmt)
            for table, column, col_type in _MIGRATIONS:
                await con.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                )

        # Warm the in-memory caches used by the guard on every update.
        now = int(time.time())
        _banned.clear()
        for r in await con.fetch("SELECT user_id, banned_until FROM users WHERE is_banned"):
            until = r["banned_until"]
            if not until or until > now:
                _banned[r["user_id"]] = until or 0
        _muted.clear()
        for r in await con.fetch(
            "SELECT user_id, muted_until FROM users WHERE muted_until > $1", now
        ):
            _muted[r["user_id"]] = r["muted_until"]
        _restrict.clear()
        for r in await con.fetch(
            "SELECT user_id, restrict_flags, daily_limit FROM users "
            "WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0"
        ):
            _restrict[r["user_id"]] = (_parse_flags(r["restrict_flags"]), int(r["daily_limit"] or 0))
        _threads.clear()
        for r in await con.fetch("SELECT user_id, owner_id FROM chat_threads WHERE active"):
            _threads[r["user_id"]] = r["owner_id"]
        _settings.clear()
        for r in await con.fetch("SELECT key, value FROM bot_settings"):
            _settings[r["key"]] = r["value"]

    await restore_files()


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# --------------------------------------------------------------------- #
# Files kept in the database (thumbnails, covers, watermark logos, session)
# --------------------------------------------------------------------- #
def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


async def store_file(path: Optional[str]) -> None:
    """Mirror the file at `path` into the database. Never raises."""
    if not path:
        return
    try:
        data = await asyncio.to_thread(_read_bytes, path)
        if data is None:
            return
        await pool().execute(
            "INSERT INTO stored_files (path, data, updated_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (path) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
            _norm(path), data, int(time.time()),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not mirror %s into the database", path)


async def store_file_as(src_path: str, key_path: str) -> None:
    """Store the bytes of `src_path` under the database key `key_path`."""
    data = await asyncio.to_thread(_read_bytes, src_path)
    if data is None:
        return
    await pool().execute(
        "INSERT INTO stored_files (path, data, updated_at) VALUES ($1, $2, $3) "
        "ON CONFLICT (path) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
        _norm(key_path), data, int(time.time()),
    )


async def forget_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        await pool().execute("DELETE FROM stored_files WHERE path = $1", _norm(path))
    except Exception:  # noqa: BLE001
        logger.exception("Could not remove %s from the database", path)


def _norm(path: str) -> str:
    return os.path.normpath(path).replace(os.sep, "/")


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


async def restore_files() -> int:
    """Write back every saved file that is missing on disk. Returns how many."""
    restored = 0
    async with pool().acquire() as con:
        rows = await con.fetch("SELECT path FROM stored_files")
        for r in rows:
            path = r["path"]
            if os.path.exists(path):
                continue
            data = await con.fetchval("SELECT data FROM stored_files WHERE path = $1", path)
            if data is None:
                continue
            try:
                await asyncio.to_thread(_write_bytes, path, bytes(data))
                restored += 1
            except OSError:
                logger.exception("Could not restore %s", path)
    if restored:
        logger.info("Restored %s saved file(s) from the database.", restored)
    return restored


async def load_file(path: str) -> Optional[bytes]:
    row = await pool().fetchrow("SELECT data FROM stored_files WHERE path = $1", _norm(path))
    return bytes(row["data"]) if row else None


# --------------------------------------------------------------------- #
# Internal upsert helpers (field names are whitelisted)
# --------------------------------------------------------------------- #
async def _set_user_field(user_id: int, field: str, value: Any) -> None:
    assert field in _USER_FIELDS, field
    await pool().execute(
        f"""
        INSERT INTO users (user_id, {field}, date_joined) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET {field} = EXCLUDED.{field}
        """,
        user_id, value, int(time.time()),
    )


async def _set_channel_field(channel_id: int, field: str, value: Any) -> None:
    assert field in _CHANNEL_FIELDS, field
    await pool().execute(
        f"""
        INSERT INTO channels (channel_id, {field}, is_active) VALUES ($1, $2, TRUE)
        ON CONFLICT (channel_id) DO UPDATE SET {field} = EXCLUDED.{field}
        """,
        channel_id, value,
    )


async def _old_path(table: str, key_col: str, key: int, col: str) -> Optional[str]:
    return await pool().fetchval(f"SELECT {col} FROM {table} WHERE {key_col} = $1", key)


async def _swap_file(old: Optional[str], new: Optional[str]) -> None:
    """Keep the database copy of an image in step with the path we now use."""
    if old and old != new:
        await forget_file(old)
    if new:
        await store_file(new)


# --------------------------------------------------------------------- #
# User operations
# --------------------------------------------------------------------- #
async def ensure_user(user_id: int) -> None:
    await pool().execute(
        "INSERT INTO users (user_id, date_joined) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
        user_id, int(time.time()),
    )


async def get_user(user_id: int) -> Optional[UserSettings]:
    row = await pool().fetchrow(f"SELECT {_USER_COLUMNS} FROM users WHERE user_id = $1", user_id)
    if row is None:
        return None
    return UserSettings(**dict(row))


async def set_user_thumbnail(user_id: int, path: str) -> None:
    old = await _old_path("users", "user_id", user_id, "thumbnail_path")
    await _set_user_field(user_id, "thumbnail_path", path)
    await _swap_file(old, path)
    # The full-quality source copy kept next to it (see handlers/thumbnail.py).
    from config import THUMB_DIR

    for src in glob.glob(os.path.join(THUMB_DIR, f"{user_id}_src.*")):
        await store_file(src)


async def delete_user_thumbnail(user_id: int) -> None:
    old = await _old_path("users", "user_id", user_id, "thumbnail_path")
    await _set_user_field(user_id, "thumbnail_path", None)
    await forget_file(old)


async def set_user_caption(user_id: int, caption: str) -> None:
    await _set_user_field(user_id, "custom_caption", caption)


async def delete_user_caption(user_id: int) -> None:
    await _set_user_field(user_id, "custom_caption", None)


async def set_user_watermark_patterns(user_id: int, patterns: str) -> None:
    await _set_user_field(user_id, "watermark_patterns", patterns)


async def delete_user_watermark_patterns(user_id: int) -> None:
    await _set_user_field(user_id, "watermark_patterns", None)


async def set_user_link(user_id: int, url: Optional[str]) -> None:
    await _set_user_field(user_id, "custom_link", url)


async def set_user_cover(user_id: int, path: str) -> None:
    old = await _old_path("users", "user_id", user_id, "cover_path")
    await _set_user_field(user_id, "cover_path", path)
    await _swap_file(old, path)


async def delete_user_cover(user_id: int) -> None:
    old = await _old_path("users", "user_id", user_id, "cover_path")
    await _set_user_field(user_id, "cover_path", None)
    await forget_file(old)


async def set_user_cover_enabled(user_id: int, enabled: bool) -> None:
    await _set_user_field(user_id, "cover_enabled", bool(enabled))


async def set_user_cover_mode(user_id: int, mode: str) -> None:
    await _set_user_field(user_id, "cover_mode", mode)


# --------------------------------------------------------------------- #
# Channel operations
# --------------------------------------------------------------------- #
async def bind_channel(channel_id: int, bound_by: int) -> None:
    await pool().execute(
        """
        INSERT INTO channels (channel_id, bound_by, is_active) VALUES ($1, $2, TRUE)
        ON CONFLICT (channel_id) DO UPDATE SET is_active = TRUE, bound_by = EXCLUDED.bound_by
        """,
        channel_id, bound_by,
    )


async def unbind_channel(channel_id: int) -> None:
    await pool().execute("UPDATE channels SET is_active = FALSE WHERE channel_id = $1", channel_id)


async def get_channel(channel_id: int) -> Optional[ChannelSettings]:
    row = await pool().fetchrow(
        f"SELECT {_CHANNEL_COLUMNS} FROM channels WHERE channel_id = $1", channel_id
    )
    return ChannelSettings(**dict(row)) if row else None


async def set_channel_thumbnail(channel_id: int, path: str) -> None:
    old = await _old_path("channels", "channel_id", channel_id, "default_thumbnail_path")
    await _set_channel_field(channel_id, "default_thumbnail_path", path)
    await _swap_file(old, path)


async def set_channel_caption(channel_id: int, caption: str) -> None:
    await _set_channel_field(channel_id, "channel_caption_template", caption)


async def set_channel_watermark_patterns(channel_id: int, patterns: str) -> None:
    await _set_channel_field(channel_id, "channel_watermark_patterns", patterns)


async def delete_channel_watermark_patterns(channel_id: int) -> None:
    await _set_channel_field(channel_id, "channel_watermark_patterns", None)


async def set_channel_cover(channel_id: int, path: str) -> None:
    old = await _old_path("channels", "channel_id", channel_id, "channel_cover_path")
    await _set_channel_field(channel_id, "channel_cover_path", path)
    await _swap_file(old, path)


async def delete_channel_cover(channel_id: int) -> None:
    old = await _old_path("channels", "channel_id", channel_id, "channel_cover_path")
    await _set_channel_field(channel_id, "channel_cover_path", None)
    await forget_file(old)


# --------------------------------------------------------------------- #
# Bot settings (key/value) — cached in memory
# --------------------------------------------------------------------- #
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    return _settings.get(key, default)


async def set_setting(key: str, value: Optional[str]) -> None:
    if value is None:
        await pool().execute("DELETE FROM bot_settings WHERE key = $1", key)
        _settings.pop(key, None)
    else:
        await pool().execute(
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value,
        )
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
    await pool().execute(
        """
        INSERT INTO users (user_id, date_joined, username, first_name, last_seen)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            last_seen = EXCLUDED.last_seen
        """,
        user_id, now, username, first_name, now,
    )


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
    await pool().execute(
        """
        INSERT INTO users (user_id, date_joined, is_banned, banned_until)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE SET
            is_banned = EXCLUDED.is_banned, banned_until = EXCLUDED.banned_until
        """,
        user_id, int(time.time()), bool(banned), until if banned else None,
    )
    if banned:
        _banned[user_id] = int(until or 0)
    else:
        _banned.pop(user_id, None)


async def set_muted(user_id: int, until: Optional[int]) -> None:
    """Mute (bot ignores the user completely) until `until`; None = unmute.
    Use until=0 for a mute with no end."""
    await pool().execute(
        """
        INSERT INTO users (user_id, date_joined, muted_until) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET muted_until = EXCLUDED.muted_until
        """,
        user_id, int(time.time()), None if until is None else until or 4102444800,
    )
    if until is None:
        _muted.pop(user_id, None)
    else:
        _muted[user_id] = int(until)


async def set_restrictions(user_id: int, flags, daily_limit: int) -> None:
    flags = frozenset(f for f in flags if f in RESTRICT_FLAGS)
    daily_limit = max(0, int(daily_limit))
    await pool().execute(
        """
        INSERT INTO users (user_id, date_joined, restrict_flags, daily_limit)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE SET
            restrict_flags = EXCLUDED.restrict_flags,
            daily_limit = EXCLUDED.daily_limit
        """,
        user_id, int(time.time()), ",".join(sorted(flags)), daily_limit,
    )
    if flags or daily_limit:
        _restrict[user_id] = (flags, daily_limit)
    else:
        _restrict.pop(user_id, None)


async def incr_files(user_id: int) -> None:
    await pool().execute(
        "UPDATE users SET files_count = COALESCE(files_count, 0) + 1 WHERE user_id = $1", user_id
    )


async def get_profile(user_id: int) -> Optional[UserProfile]:
    row = await pool().fetchrow(f"SELECT {_PROFILE_COLUMNS} FROM users WHERE user_id = $1", user_id)
    return _profile(row) if row else None


async def find_profile(query: str) -> Optional[UserProfile]:
    """Look a user up by numeric id or by exact @username."""
    query = query.strip()
    if query.lstrip("-").isdigit():
        return await get_profile(int(query))
    row = await pool().fetchrow(
        f"SELECT {_PROFILE_COLUMNS} FROM users WHERE lower(username) = lower($1)",
        query.lstrip("@"),
    )
    return _profile(row) if row else None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_profiles(query: str, limit: int = 10) -> list:
    """Id / @username / part of the name or username."""
    query = query.strip()
    if not query:
        return []
    if query.lstrip("-").isdigit():
        p = await get_profile(int(query))
        return [p] if p else []
    like = f"%{_like_escape(query.lstrip('@'))}%"
    rows = await pool().fetch(
        f"SELECT {_PROFILE_COLUMNS} FROM users "
        "WHERE username ILIKE $1 OR first_name ILIKE $1 "
        "ORDER BY COALESCE(last_seen, 0) DESC LIMIT $2",
        like, limit,
    )
    return [_profile(r) for r in rows]


# user-list filters: a=all (recent activity), n=newest, b=banned, m=muted,
# r=restricted, o=active in the last 24h
USER_FILTERS = ("a", "n", "b", "m", "r", "o")


def _filter_sql(flt: str) -> tuple:
    """(WHERE clause using $1 for the timestamp when needed, params, ORDER BY)."""
    now = int(time.time())
    if flt == "b":
        return (
            "WHERE is_banned AND (COALESCE(banned_until, 0) = 0 OR banned_until > $1)",
            (now,), "COALESCE(last_seen, 0) DESC",
        )
    if flt == "m":
        return "WHERE muted_until > $1", (now,), "muted_until ASC"
    if flt == "r":
        return (
            "WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0",
            (), "COALESCE(last_seen, 0) DESC",
        )
    if flt == "o":
        return "WHERE last_seen >= $1", (now - 86400,), "last_seen DESC"
    if flt == "n":
        return "", (), "date_joined DESC, user_id DESC"
    return "", (), "COALESCE(last_seen, 0) DESC, user_id DESC"


async def count_profiles(flt: str = "a") -> int:
    where, params, _ = _filter_sql(flt)
    return int(await pool().fetchval(f"SELECT COUNT(*) FROM users {where}", *params) or 0)


async def list_profiles(offset: int = 0, limit: int = 10, flt: str = "a") -> list:
    where, params, order = _filter_sql(flt)
    n = len(params)
    rows = await pool().fetch(
        f"SELECT {_PROFILE_COLUMNS} FROM users {where} ORDER BY {order} "
        f"LIMIT ${n + 1} OFFSET ${n + 2}",
        *params, limit, offset,
    )
    return [_profile(r) for r in rows]


async def all_profiles() -> list:
    rows = await pool().fetch(f"SELECT {_PROFILE_COLUMNS} FROM users ORDER BY date_joined ASC")
    return [_profile(r) for r in rows]


async def broadcast_ids() -> list:
    """Every user who is not banned right now."""
    now = int(time.time())
    rows = await pool().fetch(
        "SELECT user_id FROM users WHERE NOT is_banned "
        "OR (COALESCE(banned_until, 0) != 0 AND banned_until <= $1)",
        now,
    )
    return [r["user_id"] for r in rows]


async def get_stats() -> dict:
    now = int(time.time())
    day, week = 24 * 3600, 7 * 24 * 3600
    p = pool()

    async def one(sql: str, *params) -> int:
        return int(await p.fetchval(sql, *params) or 0)

    return {
        "total": await one("SELECT COUNT(*) FROM users"),
        "new_24h": await one("SELECT COUNT(*) FROM users WHERE date_joined >= $1", now - day),
        "new_7d": await one("SELECT COUNT(*) FROM users WHERE date_joined >= $1", now - week),
        "active_24h": await one("SELECT COUNT(*) FROM users WHERE last_seen >= $1", now - day),
        "banned": await one(
            "SELECT COUNT(*) FROM users WHERE is_banned "
            "AND (COALESCE(banned_until, 0) = 0 OR banned_until > $1)", now,
        ),
        "muted": await one("SELECT COUNT(*) FROM users WHERE muted_until > $1", now),
        "restricted": await one(
            "SELECT COUNT(*) FROM users WHERE COALESCE(restrict_flags, '') != '' OR daily_limit > 0"
        ),
        "files": await one("SELECT SUM(files_count) FROM users"),
        "channels": await one("SELECT COUNT(*) FROM channels WHERE is_active"),
    }


# --------------------------------------------------------------------- #
# Activity log (everything a user did with the bot / was done to him)
# --------------------------------------------------------------------- #
_KEEP_EVENTS = 3000  # per user


async def log_event(user_id: int, kind: str, detail: Optional[str] = None) -> None:
    """Never raises: logging must not break the bot."""
    try:
        await pool().execute(
            "INSERT INTO activity_log (user_id, ts, kind, detail) VALUES ($1, $2, $3, $4)",
            user_id, int(time.time()), kind, (detail or "")[:500],
        )
        if random.random() < 0.01:  # occasional trim
            await pool().execute(
                "DELETE FROM activity_log WHERE user_id = $1 AND id NOT IN "
                "(SELECT id FROM activity_log WHERE user_id = $1 ORDER BY id DESC LIMIT $2)",
                user_id, _KEEP_EVENTS,
            )
    except Exception:  # noqa: BLE001
        pass


def _kinds_sql(kinds, first_param: int) -> tuple:
    """Extra `AND kind IN (...)` clause numbered from `first_param`."""
    if not kinds:
        return "", ()
    kinds = tuple(kinds)
    marks = ",".join(f"${first_param + i}" for i in range(len(kinds)))
    return f" AND kind IN ({marks})", kinds


async def count_events(user_id: int, kinds=None) -> int:
    extra, params = _kinds_sql(kinds, 2)
    return int(
        await pool().fetchval(
            f"SELECT COUNT(*) FROM activity_log WHERE user_id = $1{extra}", user_id, *params
        ) or 0
    )


async def get_events(user_id: int, kinds=None, offset: int = 0, limit: int = 12) -> list:
    """Newest first. Rows are (ts, kind, detail)."""
    extra, params = _kinds_sql(kinds, 2)
    n = 1 + len(params)
    rows = await pool().fetch(
        f"SELECT ts, kind, detail FROM activity_log WHERE user_id = $1{extra} "
        f"ORDER BY id DESC LIMIT ${n + 1} OFFSET ${n + 2}",
        user_id, *params, limit, offset,
    )
    return [tuple(r) for r in rows]


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
    return await pool().fetchval(
        "INSERT INTO files_log (user_id, ts, orig_name, new_name, media_type, size, "
        "file_id, notes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
        user_id, int(time.time()), orig_name, new_name, media_type, size, file_id, notes,
    )


async def get_file(file_row_id: int) -> Optional[FileRecord]:
    row = await pool().fetchrow(f"{_FILE_SELECT} WHERE f.id = $1", file_row_id)
    return FileRecord(*tuple(row)) if row else None


async def count_files(user_id: Optional[int] = None) -> int:
    if user_id is None:
        return int(await pool().fetchval("SELECT COUNT(*) FROM files_log") or 0)
    return int(
        await pool().fetchval("SELECT COUNT(*) FROM files_log WHERE user_id = $1", user_id) or 0
    )


async def list_files(user_id: Optional[int] = None, offset: int = 0, limit: int = 8) -> list:
    if user_id is None:
        rows = await pool().fetch(
            f"{_FILE_SELECT} ORDER BY f.id DESC LIMIT $1 OFFSET $2", limit, offset
        )
    else:
        rows = await pool().fetch(
            f"{_FILE_SELECT} WHERE f.user_id = $1 ORDER BY f.id DESC LIMIT $2 OFFSET $3",
            user_id, limit, offset,
        )
    return [FileRecord(*tuple(r)) for r in rows]


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
    return int(
        await pool().fetchval(
            "SELECT COUNT(*) FROM files_log WHERE user_id = $1 AND ts >= $2",
            user_id, _day_start(),
        ) or 0
    )


async def file_stats(user_id: int) -> tuple:
    """(count, total bytes, last timestamp)"""
    row = await pool().fetchrow(
        "SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(ts) FROM files_log WHERE user_id = $1",
        user_id,
    )
    return int(row[0] or 0), int(row[1] or 0), row[2]


# --------------------------------------------------------------------- #
# Owner <-> user conversations
# --------------------------------------------------------------------- #
CHAT_IDLE_SECONDS = 72 * 3600  # a thread nobody writes in for this long closes itself


def thread_owner(user_id: int) -> Optional[int]:
    """Owner id if this user has an open conversation, else None. In-memory."""
    return _threads.get(user_id)


async def open_thread(user_id: int, owner_id: int) -> None:
    await pool().execute(
        """
        INSERT INTO chat_threads (user_id, owner_id, active, last_ts) VALUES ($1, $2, TRUE, $3)
        ON CONFLICT (user_id) DO UPDATE SET
            owner_id = EXCLUDED.owner_id, active = TRUE, last_ts = EXCLUDED.last_ts
        """,
        user_id, owner_id, int(time.time()),
    )
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
    await pool().execute("UPDATE chat_threads SET active = FALSE WHERE user_id = $1", user_id)


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
    row = await pool().fetchrow(
        f"SELECT {_VIDEO_COLUMNS} FROM video_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return VideoSettings(user_id=user_id)
    return VideoSettings(**dict(row))


async def update_video_settings(user_id: int, **fields: Any) -> None:
    """Upsert one or more video-setting fields atomically."""
    assert fields and set(fields) <= _VIDEO_FIELDS, fields
    cols = list(fields)
    values = list(fields.values())
    placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    old_image = None
    if "wm_image_path" in fields:
        old_image = await _old_path("video_settings", "user_id", user_id, "wm_image_path")
    await pool().execute(
        f"INSERT INTO video_settings (user_id, {', '.join(cols)}) "
        f"VALUES ($1, {placeholders}) "
        f"ON CONFLICT (user_id) DO UPDATE SET {updates}",
        user_id, *values,
    )
    if "wm_image_path" in fields:
        await _swap_file(old_image, fields["wm_image_path"])
