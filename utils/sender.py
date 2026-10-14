"""
utils/sender.py
=================
Shared helpers for talking to Telegram: FloodWait-aware retries, a global
concurrency limiter, and a single `send_media` used by both the private
flow and the channel automation.
"""

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional, TypeVar

from pyrogram import Client
from pyrogram.errors import FloodWait

import config
from utils.integrity import IntegrityError, verify_upload

logger = logging.getLogger(__name__)
T = TypeVar("T")

_semaphore: Optional[asyncio.Semaphore] = None


def job_semaphore() -> asyncio.Semaphore:
    """Created lazily so it binds to the running event loop."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
    return _semaphore


async def with_flood_retry(factory: Callable[[], Awaitable[T]], retries: int = 3) -> T:
    """Run `factory()`; on FloodWait sleep for the requested time and retry."""
    for attempt in range(retries + 1):
        try:
            return await factory()
        except FloodWait as exc:
            if attempt == retries:
                raise
            logger.warning("FloodWait: sleeping %s seconds", exc.value)
            await asyncio.sleep(exc.value + 1)
    raise RuntimeError("unreachable")


async def send_media(
    client: Client,
    chat_id: int,
    media_type: str,
    path: str,
    file_name: str,
    caption: str,
    thumb: Optional[str] = None,
    duration: Optional[int] = None,
    progress=None,
    width: Optional[int] = None,
    height: Optional[int] = None,
):
    """Send `path` as video / audio / document with FloodWait retries."""
    if media_type == "video":
        return await with_flood_retry(
            lambda: client.send_video(
                chat_id=chat_id, video=path, file_name=file_name, caption=caption,
                thumb=thumb, duration=duration or 0, progress=progress,
                width=width or 0, height=height or 0, supports_streaming=True,
            )
        )
    if media_type == "audio":
        return await with_flood_retry(
            lambda: client.send_audio(
                chat_id=chat_id, audio=path, file_name=file_name, caption=caption,
                thumb=thumb, duration=duration or 0, progress=progress,
            )
        )
    return await with_flood_retry(
        lambda: client.send_document(
            chat_id=chat_id, document=path, file_name=file_name, caption=caption,
            thumb=thumb, progress=progress,
        )
    )


async def send_verified(client: Client, chat_id: int, media_type: str, path: str, *args, **kwargs):
    """
    send_media + a check that Telegram stored exactly the bytes we sent.
    A mismatching upload is deleted and sent once more; if it is still wrong
    IntegrityError is raised (so callers never delete an original for it).
    """
    local_size = os.path.getsize(path)
    for attempt in (1, 2):
        sent = await send_media(client, chat_id, media_type, path, *args, **kwargs)
        try:
            verify_upload(sent, local_size)
            return sent
        except IntegrityError as exc:
            logger.error("Upload check failed (attempt %s): %s", attempt, exc)
            try:
                if sent is not None:
                    await sent.delete()
            except Exception:
                pass
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


_background: set = set()


def spawn(coro) -> "asyncio.Task":
    """
    Run a long job (download/edit/upload) in the background so the Pyrogram
    handler returns immediately. Otherwise every transfer would occupy one
    update worker for its whole duration and, with a few big files, the
    bot would stop answering button presses.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)  # keep a reference so it isn't garbage collected
    task.add_done_callback(_background.discard)
    return task
