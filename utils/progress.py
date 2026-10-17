"""
utils/progress.py
===================
Async-friendly progress reporting for Pyrogram's `download_media` /
`send_document` (etc.) `progress` callbacks.

Pyrogram invokes the progress callback very frequently (potentially many
times per second for fast transfers). Editing a Telegram message that often
will immediately trigger `FloodWait`. `Progress` therefore throttles edits
to at most once every `PROGRESS_EDIT_INTERVAL` seconds and swallows/handles
`FloodWait` gracefully.
"""

import asyncio
import time
from typing import Optional

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message

from config import PROGRESS_EDIT_INTERVAL
from utils import ui
from utils.helpers import human_bytes, human_time

# Unicode block characters used to render the progress bar.
_BAR_LENGTH = 12


def _make_bar(percentage: float) -> str:
    return ui.bar(percentage, _BAR_LENGTH)


class Progress:
    """
    Stateful progress tracker bound to a single Telegram status message.

    Usage:
        progress = Progress(status_message, action="Downloading")
        await client.download_media(msg, progress=progress.update)
    """

    def __init__(self, status_message: Message, action: str = "جارٍ المعالجة") -> None:
        self.status_message = status_message
        self.action = action
        self.start_time = time.time()
        self.last_edit_time: float = 0.0
        self._last_current: int = 0

    async def update(self, current: int, total: int) -> None:
        """
        Callback signature matches what Pyrogram expects: (current, total).
        Safe to pass directly as the `progress=` kwarg.
        """
        now = time.time()

        # Throttle: only edit the message at most once per interval, and
        # always allow the final (100%) update through.
        is_final = current >= total
        if not is_final and (now - self.last_edit_time) < PROGRESS_EDIT_INTERVAL:
            return

        elapsed = max(now - self.start_time, 1e-6)
        speed = current / elapsed  # bytes/sec
        percentage = (current / total) * 100 if total else 0
        eta = (total - current) / speed if speed > 0 else 0

        text = ui.card(
            "⚡️",
            self.action,
            percentage,
            f"📦 **الحجم** ‹ {human_bytes(current)} / {human_bytes(total)}\n"
            f"🚀 **السرعة** ‹ {human_bytes(speed)}/ث\n"
            f"⏱ **المتبقي** ‹ {human_time(eta)}",
        )

        try:
            await self.status_message.edit_text(text)
            self.last_edit_time = now
        except FloodWait as e:
            # Respect Telegram's cooldown, then simply skip this update —
            # the next throttled call will catch the UI up.
            self.last_edit_time = now + e.value
        except MessageNotModified:
            # Text was identical to the previous edit; not an error.
            self.last_edit_time = now
        except Exception:
            # Never let a UI update failure abort the actual transfer.
            pass


async def noop_progress(current: int, total: int) -> None:
    """A do-nothing progress callback, useful when UI updates are disabled."""
    return


class WatermarkProgress:
    """
    Progress reporter for the watermark-redaction pass, which runs
    synchronously inside a thread-pool executor (see utils.pdf_watermark)
    rather than as a Pyrogram transfer — so unlike `Progress` above, the
    callback handed to it (`report`) must be a plain, non-async function
    safe to call from a worker thread. It schedules a throttled message
    edit back onto the event loop via `run_coroutine_threadsafe`.

    Matters mainly for big books (hundreds of pages): without this, the
    status message would just say "removing watermark..." with no
    indication of progress for however long the pass takes.
    """

    def __init__(
        self,
        status_message: Message,
        loop: asyncio.AbstractEventLoop,
        action: str = "🧽 جارٍ إزالة العلامة المائية",
    ) -> None:
        self.status_message = status_message
        self.loop = loop
        self.action = action
        self.last_edit_time: float = 0.0

    def report(self, pages_done: int, total_pages: int) -> None:
        """Sync callback — safe to call from a non-event-loop thread."""
        now = time.time()
        is_final = pages_done >= total_pages
        if not is_final and (now - self.last_edit_time) < PROGRESS_EDIT_INTERVAL:
            return
        self.last_edit_time = now

        percentage = (pages_done / total_pages) * 100 if total_pages else 0
        text = ui.card(
            "🧽",
            self.action,
            percentage,
            f"📚 **الصفحات** ‹ {pages_done} / {total_pages}",
        )
        # Fire-and-forget: scheduling from a worker thread, so we can't
        # await here. `_safe_edit` swallows its own errors.
        asyncio.run_coroutine_threadsafe(self._safe_edit(text), self.loop)

    async def _safe_edit(self, text: str) -> None:
        try:
            await self.status_message.edit_text(text)
        except Exception:
            # Never let a UI update failure abort the actual redaction pass
            # running in parallel on the executor thread. Covers
            # FloodWait/MessageNotModified too — this is best-effort UI only.
            pass


class VideoProgress:
    """Throttled status-message updates while ffmpeg processes a video."""

    def __init__(self, status_message: Message, action: str = "🎬 جارٍ معالجة الفيديو") -> None:
        self.status_message = status_message
        self.action = action
        self.start_time = time.time()
        self.last_edit_time: float = 0.0

    async def update(self, done: float, total: float) -> None:
        now = time.time()
        is_final = total > 0 and done >= total
        if not is_final and (now - self.last_edit_time) < PROGRESS_EDIT_INTERVAL:
            return
        percentage = (done / total) * 100 if total else 0
        elapsed = max(now - self.start_time, 1e-6)
        speed = done / elapsed  # seconds of video per second
        eta = (total - done) / speed if speed > 0 else 0
        text = ui.card(
            "🎬",
            self.action,
            percentage,
            f"⏱ **المتبقي** ‹ {human_time(eta)}",
        )
        try:
            await self.status_message.edit_text(text)
            self.last_edit_time = now
        except FloodWait as e:
            self.last_edit_time = now + e.value
        except MessageNotModified:
            self.last_edit_time = now
        except Exception:
            pass
