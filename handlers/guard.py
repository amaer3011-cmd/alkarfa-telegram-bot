"""
handlers/guard.py
===================
Runs in handler group -1, i.e. before every other handler:

1. Records / refreshes the user (name, @username, last seen) so the admin
   panel can list them, and writes every message / button press to the
   user's activity log.
2. Banned users (permanent or timed) get the "not free for you" message.
3. Muted users are ignored completely, as if the bot were switched off.
4. Everybody except the owners is blocked while maintenance mode is on.
5. Per-user restrictions set from the admin panel (no files / no videos /
   no PDFs / no settings / a daily file limit).

The owners are never blocked.
"""

import logging
import time
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

import config
from database import db
from utils.state import UserState, get_state

logger = logging.getLogger(__name__)

DEFAULT_BAN_TEXT = "ها  مش فاضيالك 💕   الكارف رجعلي ❤🙃"
MAINTENANCE_TEXT = "🛠 الكارفة في صيانة دلوقتي، جرّب كمان شوية 💕"
RESTRICTED_TEXT = "🔒 الميزة دي متقفلة عليك دلوقتي 💕"

# Button groups that count as "settings" for the `settings` restriction.
_SETTINGS_PREFIXES = (
    "menu:thumb", "menu:caption", "menu:cover", "menu:watermark", "menu:video", "menu:channel",
    "menu:link", "thumb:", "caption:", "watermark:", "cover:", "video:", "channel:", "link:",
)

# Don't write to the database on every single message of the same user.
_TOUCH_EVERY = 60  # seconds
_last_touch: dict = {}
# Don't answer every message of a banned user (avoids flood / ping-pong).
_BAN_REPLY_EVERY = 5  # seconds
_last_ban_reply: dict = {}


def maintenance_on() -> bool:
    return db.get_setting("maintenance") == "1"


def ban_text() -> str:
    """The message a banned user receives. The owner can change it."""
    return db.get_setting("ban_text") or DEFAULT_BAN_TEXT


async def _record(user) -> None:
    now = time.monotonic()
    if now - _last_touch.get(user.id, -_TOUCH_EVERY) < _TOUCH_EVERY:
        return
    _last_touch[user.id] = now
    try:
        await db.touch_user(user.id, user.username, user.first_name)
    except Exception:  # never let bookkeeping break the bot
        logger.exception("Could not record user %s", user.id)


def _file_kind(message: Message) -> Optional[str]:
    """'video' | 'pdf' | 'audio' | 'doc' for a file the user wants edited."""
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    doc = message.document
    if doc:
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        if mime.startswith("image/") and get_state(message.from_user.id).state != UserState.NONE:
            return None  # an image the user is sending to a settings flow
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "pdf"
        if mime.startswith("video/"):
            return "video"
        return "doc"
    return None


def describe_message(message: Message) -> str:
    if message.text:
        return "نص: " + message.text[:150]
    for attr, label in (
        ("document", "ملف"), ("video", "فيديو"), ("audio", "صوت"),
    ):
        media = getattr(message, attr, None)
        if media:
            return f"{label}: {getattr(media, 'file_name', None) or '—'}"
    for attr, label in (
        ("photo", "صورة"), ("voice", "رسالة صوتية"), ("sticker", "ستيكر"),
        ("video_note", "فيديو دائري"), ("animation", "GIF"), ("contact", "جهة اتصال"),
        ("location", "موقع"),
    ):
        if getattr(message, attr, None):
            return label
    return "رسالة"


async def _check_message(message: Message) -> tuple:
    """(reason, reply) — reason None means the message may go through."""
    uid = message.from_user.id
    if db.is_banned(uid):
        return "محظور", ban_text()
    if db.is_muted(uid):
        return "مكتوم", None  # silence
    if maintenance_on():
        return "صيانة", MAINTENANCE_TEXT

    flags, limit = db.get_restrictions(uid)
    if not flags and not limit:
        return None, None
    kind = _file_kind(message)
    if kind is None:
        return None, None
    if (
        "files" in flags
        or (kind == "video" and "video" in flags)
        or (kind == "pdf" and "pdf" in flags)
    ):
        return "تقييد", RESTRICTED_TEXT
    if limit and await db.files_today(uid) >= limit:
        return "حد يومي", f"🚦 خلّصت حدّك النهاردة ({limit} ملف). كمّل بكرة 💕"
    return None, None


def _check_callback(cq: CallbackQuery) -> tuple:
    uid = cq.from_user.id
    if db.is_banned(uid):
        return "محظور", ban_text()
    if db.is_muted(uid):
        return "مكتوم", None
    if maintenance_on():
        return "صيانة", MAINTENANCE_TEXT
    flags, _limit = db.get_restrictions(uid)
    if not flags:
        return None, None
    data = cq.data or ""
    if "settings" in flags and data.startswith(_SETTINGS_PREFIXES):
        return "تقييد", RESTRICTED_TEXT
    if "files" in flags and data.startswith(("rename:", "studio:")):
        return "تقييد", RESTRICTED_TEXT
    if "pdf" in flags and data.startswith("studio:"):
        return "تقييد", RESTRICTED_TEXT
    return None, None


def register(app: Client) -> None:
    @app.on_message(filters.private, group=-1)
    async def guard_message(client: Client, message: Message) -> None:
        user = message.from_user
        if user is None or config.is_owner(user.id):
            return
        await _record(user)
        reason, reply = await _check_message(message)
        if reason is None:
            await db.log_event(
                user.id, "file_in" if _file_kind(message) else "msg", describe_message(message)
            )
            return
        await db.log_event(user.id, "blocked", f"{reason} — {describe_message(message)}")
        if reply:
            now = time.monotonic()
            if reason != "محظور" or now - _last_ban_reply.get(user.id, -_BAN_REPLY_EVERY) >= _BAN_REPLY_EVERY:
                _last_ban_reply[user.id] = now
                try:
                    await message.reply_text(reply, parse_mode=ParseMode.DISABLED)
                except Exception:
                    logger.exception("Could not answer blocked user %s", user.id)
        message.stop_propagation()

    @app.on_callback_query(group=-1)
    async def guard_callback(client: Client, cq: CallbackQuery) -> None:
        user = cq.from_user
        if user is None or config.is_owner(user.id):
            return
        await _record(user)
        reason, reply = _check_callback(cq)
        if reason is None:
            await db.log_event(user.id, "cb", cq.data or "")
            return
        await db.log_event(user.id, "blocked", f"{reason} — زر: {cq.data}")
        try:
            if reply:
                await cq.answer(reply, show_alert=True)
            else:
                await cq.answer()
        except Exception:
            pass
        cq.stop_propagation()
