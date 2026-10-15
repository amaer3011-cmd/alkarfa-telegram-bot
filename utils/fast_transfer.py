"""
utils/fast_transfer.py
========================
Faster Telegram transfers (the stock Pyrogram/Kurigram ones are slow on a
high-latency host such as Railway).

Why it is slow
--------------
* download_media asks for the file ONE 1 MB chunk at a time and waits for each
  answer before asking for the next, so the speed is capped at
  (1 MB / round-trip time) — a few MB/s whatever the bandwidth is.
* uploads use 4 parallel part-workers; more of them keep the pipe fuller.

What this module does
---------------------
* fast_download(): requests many 1 MB chunks at once (FAST_DOWNLOAD_WORKERS in
  flight) over Telegram's media connection and writes each at its offset.
  Anything unusual (photo, CDN redirect, expired file reference, any error)
  falls back to the normal download_media, so it can only be faster, never
  less reliable.
* patch_upload_workers(): raises the upload part-workers from 4 to
  FAST_UPLOAD_WORKERS by re-compiling Kurigram's own save_file with that one
  number changed. If the library's code ever changes shape the patch is simply
  skipped (logged) and uploads keep working as before.
"""

import asyncio
import inspect
import logging
import math
import os
import textwrap
from typing import Callable, Optional

from pyrogram import Client, raw
from pyrogram.file_id import FileId, FileType

import config

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # Telegram's maximum (and the alignment unit) for GetFile
MIN_PARALLEL_BYTES = 4 * CHUNK  # below this the overhead isn't worth it
_MEDIA_ATTRS = ("document", "video", "audio", "voice", "animation", "video_note")
_OK_TYPES = {
    FileType.DOCUMENT, FileType.VIDEO, FileType.AUDIO,
    FileType.VOICE, FileType.ANIMATION, FileType.VIDEO_NOTE,
}


class _Fallback(Exception):
    """Raised when the parallel path can't be used for this file."""


def _media_of(message):
    for attr in _MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is not None:
            return media
    return None


async def _report(progress: Optional[Callable], current: int, total: int) -> None:
    if progress is None:
        return
    try:
        result = progress(current, total)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # A broken progress message must never abort a transfer.
        logger.debug("progress callback failed", exc_info=True)


async def _parallel_get(client: Client, message, dest: str, progress, workers: int) -> str:
    media = _media_of(message)
    if media is None:
        raise _Fallback("no downloadable media")
    size = int(getattr(media, "file_size", 0) or 0)
    if size < MIN_PARALLEL_BYTES:
        raise _Fallback("small file")
    fid = FileId.decode(media.file_id)
    if fid.file_type not in _OK_TYPES:
        raise _Fallback(f"unsupported type {fid.file_type}")

    location = raw.types.InputDocumentFileLocation(
        id=fid.media_id,
        access_hash=fid.access_hash,
        file_reference=fid.file_reference,
        thumb_size=fid.thumbnail_size,
    )
    session = await client.get_session(fid.dc_id, is_media=True)

    total_parts = math.ceil(size / CHUNK)
    next_part = 0
    done_bytes = 0
    failure: Optional[BaseException] = None

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    fd = os.open(dest, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)

    async def fetch(index: int) -> bytes:
        offset = index * CHUNK
        expected = min(CHUNK, size - offset)
        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            try:
                r = await session.invoke(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=CHUNK)
                )
                if not isinstance(r, raw.types.upload.File):
                    raise _Fallback("CDN redirect")
                data = r.bytes
                if len(data) != expected:
                    raise IOError(f"part {index}: got {len(data)} bytes, expected {expected}")
                return data
            except _Fallback:
                raise
            except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_exc or IOError(f"part {index} failed")

    async def worker() -> None:
        nonlocal next_part, done_bytes, failure
        while failure is None:
            index = next_part
            if index >= total_parts:
                return
            next_part += 1
            try:
                data = await fetch(index)
                os.pwrite(fd, data, index * CHUNK)
            except Exception as exc:  # recorded, then re-raised after the workers stop
                if failure is None:
                    failure = exc
                return
            done_bytes += len(data)
            await _report(progress, done_bytes, size)

    try:
        tasks = [asyncio.ensure_future(worker()) for _ in range(max(1, min(workers, total_parts)))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
        if failure is not None:
            raise failure
    except BaseException:
        os.close(fd)
        fd = -1
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)

    if os.path.getsize(dest) != size:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise IOError("downloaded size does not match the file size")
    return dest


async def fast_download(
    client: Client,
    message,
    dest: str,
    progress: Optional[Callable] = None,
    workers: Optional[int] = None,
) -> str:
    """Download `message`'s media to `dest`; returns the path (like download_media)."""
    workers = workers or config.FAST_DOWNLOAD_WORKERS
    if workers > 1:
        try:
            return await _parallel_get(client, message, dest, progress, workers)
        except _Fallback as exc:
            logger.debug("parallel download skipped: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("parallel download failed (%s: %s); using the standard one",
                           type(exc).__name__, exc)
    return await client.download_media(message, file_name=dest, progress=progress)


def patch_upload_workers(workers: Optional[int] = None) -> bool:
    """Raise Kurigram's per-upload part workers (4 -> `workers`). Returns True if applied."""
    workers = int(workers or config.FAST_UPLOAD_WORKERS)
    if workers <= 4:
        return False
    try:
        from pyrogram.methods.advanced import save_file as module

        cls = module.SaveFile
        original = cls.save_file
        source = textwrap.dedent(inspect.getsource(original))
        needle = "workers_count = 4 if is_big else 1"
        if needle not in source:
            logger.warning("Upload speed patch skipped: Kurigram's save_file changed shape")
            return False
        source = source.replace(needle, f"workers_count = {workers} if is_big else 1")
        namespace: dict = {}
        exec(compile(source, module.__file__, "exec"), module.__dict__, namespace)
        patched = namespace["save_file"]
        cls.save_file = patched
        # Client.save_file is not SaveFile.save_file: pyrogram.sync wrapped the
        # ORIGINAL function around it at import time, so wrap the new one too.
        from pyrogram import sync
        from pyrogram.methods import Methods

        Methods.save_file = patched
        sync.async_to_sync(Methods, "save_file")
        logger.info("Upload speed patch applied (%s part workers).", workers)
        return True
    except Exception:
        logger.exception("Upload speed patch failed; uploads use the library default")
        return False
