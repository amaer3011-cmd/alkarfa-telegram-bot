"""
utils/session_sync.py
======================
Keeps the Telegram session file (`<SESSION_NAME>.session`) in PostgreSQL.

Why: the session holds the bot's login *and* the cache of users/chats it has
met. On Railway without a Volume the file disappears on every redeploy, and
the bot would then be unable to message people it hasn't heard from since.
The file is restored before the client starts, then backed up every few
minutes and once more on shutdown.
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
from typing import Optional

import config
from database import db

logger = logging.getLogger(__name__)

SESSION_FILE: str = os.path.join(config.DATA_DIR, f"{config.SESSION_NAME}.session")
BACKUP_EVERY: int = 300  # seconds


async def restore() -> None:
    """Call after db.init_db() and before the Client is created."""
    if os.path.exists(SESSION_FILE):
        return
    try:
        data = await db.load_file(SESSION_FILE)
        if data:
            await asyncio.to_thread(db._write_bytes, SESSION_FILE, data)
            logger.info("Telegram session restored from the database.")
    except Exception:  # noqa: BLE001
        logger.exception("Could not restore the Telegram session")


def _snapshot() -> Optional[str]:
    """Consistent copy of the live SQLite session into a temp file."""
    if not os.path.exists(SESSION_FILE):
        return None
    fd, tmp = tempfile.mkstemp(suffix=".session")
    os.close(fd)
    src = dst = None
    try:
        src = sqlite3.connect(SESSION_FILE, timeout=10)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        return tmp
    except Exception:  # noqa: BLE001
        logger.exception("Could not snapshot the session file")
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    finally:
        for con in (dst, src):
            if con is not None:
                con.close()


async def backup() -> None:
    tmp = await asyncio.to_thread(_snapshot)
    if not tmp:
        return
    try:
        await db.store_file_as(tmp, SESSION_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def run_forever() -> None:
    while True:
        await asyncio.sleep(BACKUP_EVERY)
        try:
            await backup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Session backup failed")
