"""
handlers/rename.py
====================
The heart of the bot: receive a file -> offer buttons -> download with a
live progress bar -> edit (PDF only) -> re-upload with the user's
thumbnail/caption.

PDF extras
-----------
For PDFs the buttons also let the user choose, for THIS file only:
    * an image to put on the first page,
    * whether it is added as a new page or replaces page 1,
    * or to skip the image / use their saved one.
If the user has a saved image with auto-apply on, it is used by default.

Several files can be pending at once: every pending file is keyed by
(user_id, message_id) and that id is baked into each button's callback.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from config import COVER_DIR, DOWNLOAD_DIR, MAX_FILE_SIZE, PENDING_TTL, THUMB_DIR
from database import db
from handlers.cover import QUALITY_TIP, save_cover_from_message
from handlers import studio as studio_handlers
from handlers import video as video_handlers
from handlers import watermark as watermark_handlers
from handlers.thumbnail import offer_thumb_confirmation
from utils.helpers import (
    default_file_name,
    ensure_extension,
    file_extension,
    get_media_duration,
    human_bytes,
    render_caption,
    sanitize_filename,
)
from utils import ui
from utils.integrity import verify_download
from utils.keyboards import cancel_kb, rename_prompt_kb
from utils.pdf_cover import (
    MODE_LABELS,
    next_mode,
    normalize_mode,
    render_first_page_thumbnail,
)
from utils.pdf_optimize import DEFAULT_LEVEL, optimize_pdf
from utils.pdf_pipeline import process_pdf
from utils import video_edit as ve
from utils.progress import Progress, VideoProgress, WatermarkProgress
from utils.fast_transfer import fast_download
from utils.sender import job_semaphore, send_verified, spawn, with_flood_retry
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)

# Cover choices for a single file
COVER_DEFAULT = "default"  # follow the user's saved settings
COVER_CUSTOM = "custom"    # an image chosen for this file only
COVER_SAVED = "saved"      # force the saved image even if auto-apply is off
COVER_NONE = "none"        # no image for this file

# Video watermark choices for a single file
WM_DEFAULT = "default"  # follow the user's video settings
WM_ON = "on"
WM_OFF = "off"

_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".3gp"}

# Set by handlers/common.py: lets image *documents* reach the photo flows.
image_document_hook: Optional[Callable] = None


@dataclass
class PendingFile:
    message: Message
    file_name: str
    file_size: int
    media_type: str  # "document" | "video" | "audio"
    created_at: float
    mime_type: str = ""
    cover_choice: str = COVER_DEFAULT
    custom_cover_path: Optional[str] = None
    cover_mode: Optional[str] = None  # None -> user's default mode
    wm_choice: str = WM_DEFAULT       # video watermark for this file
    video_title: Optional[str] = None  # None -> default template, "" -> no title
    optimize: bool = False            # PDF: improve quality / shrink size for this file

    @property
    def is_video(self) -> bool:
        if self.media_type == "video":
            return True
        return self.media_type == "document" and (
            self.mime_type.startswith("video/")
            or file_extension(self.file_name).lower() in _VIDEO_EXTS
        )

    @property
    def is_pdf(self) -> bool:
        return (
            self.media_type == "document"
            and (
                file_extension(self.file_name).lower() == ".pdf"
                or self.mime_type == "application/pdf"
            )
        )


_pending: Dict[Tuple[int, int], PendingFile] = {}


# --------------------------------------------------------------------- #
# Pending-file bookkeeping
# --------------------------------------------------------------------- #
def _drop(key: Tuple[int, int]) -> Optional[PendingFile]:
    pending = _pending.pop(key, None)
    if pending and pending.custom_cover_path and os.path.exists(pending.custom_cover_path):
        try:
            os.remove(pending.custom_cover_path)
        except OSError:
            pass
    return pending


def _purge_expired() -> None:
    now = time.time()
    for key, pending in list(_pending.items()):
        if now - pending.created_at > PENDING_TTL:
            _drop(key)


def clear_pending(user_id: int) -> None:
    """Used by the generic cancel button: forget all of this user's files."""
    for key in [k for k in _pending if k[0] == user_id]:
        _drop(key)


def _extract_file_info(message: Message) -> Optional[PendingFile]:
    for attr, media_type in (("document", "document"), ("video", "video"), ("audio", "audio")):
        media = getattr(message, attr, None)
        if media:
            mime = getattr(media, "mime_type", None) or ""
            name = getattr(media, "file_name", None) or default_file_name(message.id, mime, media_type)
            return PendingFile(
                message=message,
                file_name=name,
                file_size=media.file_size or 0,
                media_type=media_type,
                created_at=time.time(),
                mime_type=mime,
            )
    return None


# --------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------- #
def _has_file(path: Optional[str]) -> bool:
    return bool(path and os.path.exists(path))


def _resolve_cover(pending: PendingFile, user) -> Tuple[Optional[str], str]:
    """Which image (if any) and mode apply to this file."""
    mode = normalize_mode(pending.cover_mode or (user.cover_mode if user else None))
    if not pending.is_pdf or pending.cover_choice == COVER_NONE:
        return None, mode
    if pending.cover_choice == COVER_CUSTOM and _has_file(pending.custom_cover_path):
        return pending.custom_cover_path, mode
    saved = user.cover_path if user and _has_file(user.cover_path) else None
    if pending.cover_choice == COVER_SAVED:
        return saved, mode
    if pending.cover_choice == COVER_DEFAULT and saved and user.cover_enabled:
        return saved, mode
    return None, mode


def _effective_video(
    pending: PendingFile, vs: "db.VideoSettings", filename: str
) -> Tuple[Optional["ve.WatermarkSpec"], Optional[str]]:
    """The watermark and title that will really be applied to this video."""
    wanted = vs.wm_enabled if pending.wm_choice == WM_DEFAULT else pending.wm_choice == WM_ON
    wm = video_handlers.spec_from_settings(vs) if wanted and video_handlers.wm_configured(vs) else None
    if pending.video_title is not None:
        title = pending.video_title or None
    else:
        title = video_handlers.render_title(vs.title_template, filename)
    return wm, title


def _inline(text: str) -> str:
    """Safe to wrap in markdown backticks."""
    return text.replace("`", "'").replace("\n", " ")


async def _render_prompt(pending: PendingFile, user, msg_id: int):
    text = (
        ui.header("📂", "ملفك وصل")
        + f"\n📄 **الاسم** ‹ `{pending.file_name}`\n"
        f"📦 **الحجم** ‹ {human_bytes(pending.file_size)}\n"
    )
    vs = None
    wm = title = None
    if pending.is_video:
        vs = await db.get_video_settings(pending.message.from_user.id)
        wm, title = _effective_video(pending, vs, pending.file_name)
        if wm:
            what = f"نص `{_inline(wm.text)}`" if wm.mode == "text" else "لوجو"
            text += f"\n💧 **العلامة المائية:** ✅ {what}\n"
        elif not video_handlers.wm_configured(vs):
            text += "\n💧 **العلامة المائية:** غير مضبوطة (من «🎬 إعدادات الفيديو»)\n"
        else:
            text += "\n💧 **العلامة المائية:** 🚫 بدون\n"
        text += f"🏷 **العنوان:** `{_inline(title)}`\n" if title else "🏷 **العنوان:** بدون\n"
    cover_path, mode = _resolve_cover(pending, user)
    has_saved = bool(user and _has_file(user.cover_path))
    if pending.is_pdf:
        if cover_path:
            source = "صورة اخترتها لهذا الملف" if cover_path == pending.custom_cover_path else "صورتك المحفوظة"
            text += f"\n🖼 **أول صفحة:** ✅ {source}\n📌 **الوضع:** {MODE_LABELS[mode]}\n"
        else:
            text += "\n🖼 **أول صفحة:** بدون تعديل\n"
        text += (
            "✨ **تحسين الجودة وأقل حجم:** ✅ مفعّل\n" if pending.optimize
            else "✨ **تحسين الجودة وأقل حجم:** غير مفعّل\n"
        )
    text += f"\n{ui.THIN}\n✨ اختار اللي تحبه من الأزرار 👇"
    kb = rename_prompt_kb(
        msg_id,
        is_pdf=pending.is_pdf,
        cover_active=bool(cover_path),
        has_custom_cover=_has_file(pending.custom_cover_path),
        has_saved_cover=has_saved,
        mode_label=MODE_LABELS[mode],
        is_video=pending.is_video,
        wm_active=wm is not None,
        title_active=title is not None,
        optimize_active=pending.optimize,
    )
    return text, kb


async def _refresh_prompt(message: Message, user_id: int, msg_id: int) -> None:
    pending = _pending.get((user_id, msg_id))
    if pending is None:
        return
    user = await db.get_user(user_id)
    text, kb = await _render_prompt(pending, user, msg_id)
    try:
        await message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


# --------------------------------------------------------------------- #
# Text / photo flows dispatched by handlers/common.py
# --------------------------------------------------------------------- #
async def process_filename(client: Client, message: Message) -> None:
    """state == AWAITING_FILENAME."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    msg_id = entry.data.get("msg_id")
    clear_state(user_id)
    pending = _pending.pop((user_id, msg_id), None) if msg_id is not None else None

    if pending is None:
        await message.reply_text("⚠️ لا يوجد ملف قيد الانتظار. أرسل الملف مرة أخرى.")
        return

    new_name = sanitize_filename(message.text)
    if not new_name:
        _pending[(user_id, msg_id)] = pending
        await message.reply_text("⚠️ اسم غير صالح. أرسل اسمًا آخر من زر «إعادة تسمية».")
        return
    new_name = ensure_extension(new_name, pending.file_name)

    spawn(_process_and_reupload(client, message, pending, new_name))


async def process_file_cover(client: Client, message: Message) -> None:
    """state == AWAITING_FILE_COVER: the user sent the image for one file."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    msg_id = entry.data.get("msg_id")
    prompt: Optional[Message] = entry.data.get("prompt")
    clear_state(user_id)

    pending = _pending.get((user_id, msg_id))
    if pending is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.")
        return

    status = await message.reply_text("⏳ جارٍ تجهيز الصورة...")
    stem = os.path.join(COVER_DIR, f"tmp_{user_id}_{msg_id}")
    try:
        dest = await save_cover_from_message(client, message, stem)
    except Exception as exc:
        logger.exception("File cover failed for user %s", user_id)
        await status.edit_text(f"❌ فشل تجهيز الصورة: {exc}")
        return

    pending.custom_cover_path = dest
    pending.cover_choice = COVER_CUSTOM
    await status.delete()

    user = await db.get_user(user_id)
    text, kb = await _render_prompt(pending, user, msg_id)
    if prompt is not None:
        try:
            await prompt.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=kb)


async def process_file_video_title(client: Client, message: Message) -> None:
    """state == AWAITING_FILE_VIDEO_TITLE: the title for one video."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    msg_id = entry.data.get("msg_id")
    prompt: Optional[Message] = entry.data.get("prompt")
    clear_state(user_id)

    pending = _pending.get((user_id, msg_id))
    if pending is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.")
        return
    title = ve.sanitize_title(message.text or "")
    if not title:
        await message.reply_text("⚠️ العنوان فاضي. اضغط «عنوان لهذا الفيديو» وجرّب تاني.")
        return
    pending.video_title = title

    user = await db.get_user(user_id)
    text, kb = await _render_prompt(pending, user, msg_id)
    if prompt is not None:
        try:
            await prompt.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=kb)


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #
def _parse_cb(cq: CallbackQuery) -> int:
    return int(cq.data.rsplit(":", 1)[1])


async def offer_file_prompt(client: Client, message: Message) -> None:
    """Register `message` as a pending file and show the rename/PDF buttons."""
    pending = _extract_file_info(message)
    if pending is None:
        return

    if pending.file_size and pending.file_size > MAX_FILE_SIZE:
        await message.reply_text(
            f"⚠️ الملف كبير جدًا ({human_bytes(pending.file_size)}). "
            f"الحد الأقصى المدعوم هو {human_bytes(MAX_FILE_SIZE)}."
        )
        return

    user_id = message.from_user.id
    _purge_expired()
    # A fresh file overrides whatever "awaiting X" step was in progress.
    clear_state(user_id)
    _pending[(user_id, message.id)] = pending

    user = await db.get_user(user_id)
    text, kb = await _render_prompt(pending, user, message.id)
    await message.reply_text(text, reply_markup=kb)


def register(app: Client) -> None:
    @app.on_message(filters.private & (filters.document | filters.video | filters.audio))
    async def incoming_file(client: Client, message: Message) -> None:
        await db.ensure_user(message.from_user.id)

        # Inside the PDF studio (merge / split / images...): it takes the file.
        if await studio_handlers.on_document(client, message):
            return

        # A PDF sent while the user is previewing their watermark rules.
        if (
            get_state(message.from_user.id).state == UserState.AWAITING_WATERMARK_PREVIEW_PDF
            and message.document
        ):
            await watermark_handlers.process_preview_pdf(client, message)
            return

        # An image sent as a *file* while the bot waits for a photo.
        if (
            image_document_hook is not None
            and message.document
            and (message.document.mime_type or "").startswith("image/")
            and await image_document_hook(client, message)
        ):
            return

        # An image sent as a file with no flow pending: it may be meant as
        # the thumbnail / PDF first-page image (full quality), or as a plain
        # file to rename — ask.
        if message.document and (message.document.mime_type or "").startswith("image/"):
            await offer_thumb_confirmation(client, message)
            return

        await offer_file_prompt(client, message)

    @app.on_callback_query(filters.regex(r"^thumb:as_file$"))
    async def image_as_file_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        entry = get_state(user_id)
        source = entry.data.get("message")
        clear_state(user_id)
        if entry.state != UserState.AWAITING_THUMB_CONFIRM or source is None:
            await cq.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
            return
        await cq.message.delete()
        await offer_file_prompt(client, source)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:ask:\d+$"))
    async def rename_ask_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        if (user_id, msg_id) not in _pending:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        set_state(user_id, UserState.AWAITING_FILENAME, msg_id=msg_id)
        await cq.message.edit_text(
            "✏️ أرسل الاسم الجديد للملف (مع الامتداد إن أمكن).", reply_markup=cancel_kb()
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:keep:\d+$"))
    async def rename_keep_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        pending = _pending.pop((user_id, msg_id), None)
        if pending is None:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        clear_state(user_id)
        await cq.answer()
        spawn(
            _process_and_reupload(
                client, cq.message, pending,
                ensure_extension(sanitize_filename(pending.file_name), pending.file_name),
            )
        )

    @app.on_callback_query(filters.regex(r"^rename:cancel:\d+$"))
    async def rename_cancel_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        _drop((user_id, msg_id))
        clear_state(user_id)
        await cq.message.edit_text(ui.cancelled())
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:cover:\d+$"))
    async def rename_cover_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        if (user_id, msg_id) not in _pending:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        set_state(user_id, UserState.AWAITING_FILE_COVER, msg_id=msg_id, prompt=cq.message)
        await cq.message.edit_text(
            "🖼 أرسل الآن الصورة التي تريد وضعها في **أول صفحة** من هذا الملف.\n\n"
            + QUALITY_TIP,
            reply_markup=cancel_kb(),
        )
        await cq.answer()

    async def _change(cq: CallbackQuery, mutate: Callable[[PendingFile, object], Optional[str]]) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        pending = _pending.get((user_id, msg_id))
        if pending is None:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        user = await db.get_user(user_id)
        error = mutate(pending, user)
        if error:
            await cq.answer(error, show_alert=True)
            return
        await _refresh_prompt(cq.message, user_id, msg_id)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:nocover:\d+$"))
    async def rename_nocover_cb(client: Client, cq: CallbackQuery) -> None:
        def mutate(p: PendingFile, user) -> None:
            p.cover_choice = COVER_NONE
        await _change(cq, mutate)

    @app.on_callback_query(filters.regex(r"^rename:usesaved:\d+$"))
    async def rename_usesaved_cb(client: Client, cq: CallbackQuery) -> None:
        def mutate(p: PendingFile, user) -> Optional[str]:
            if not (user and _has_file(user.cover_path)):
                return "لا توجد صورة محفوظة."
            p.cover_choice = COVER_SAVED
            return None
        await _change(cq, mutate)

    @app.on_callback_query(filters.regex(r"^rename:mode:\d+$"))
    async def rename_mode_cb(client: Client, cq: CallbackQuery) -> None:
        def mutate(p: PendingFile, user) -> None:
            current = p.cover_mode or (user.cover_mode if user else None)
            p.cover_mode = next_mode(current)
        await _change(cq, mutate)

    @app.on_callback_query(filters.regex(r"^rename:opt:\d+$"))
    async def rename_opt_cb(client: Client, cq: CallbackQuery) -> None:
        def mutate(p: PendingFile, user) -> None:
            p.optimize = not p.optimize
        await _change(cq, mutate)

    @app.on_callback_query(filters.regex(r"^rename:vwm:\d+$"))
    async def rename_vwm_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        pending = _pending.get((user_id, msg_id))
        if pending is None:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        vs = await db.get_video_settings(user_id)
        wm, _title = _effective_video(pending, vs, pending.file_name)
        if wm is not None:
            pending.wm_choice = WM_OFF
        elif video_handlers.wm_configured(vs):
            pending.wm_choice = WM_ON
        else:
            await cq.answer("اضبطي العلامة الأول من «🎬 إعدادات الفيديو» في القائمة الرئيسية.", show_alert=True)
            return
        await _refresh_prompt(cq.message, user_id, msg_id)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:vtitle:\d+$"))
    async def rename_vtitle_cb(client: Client, cq: CallbackQuery) -> None:
        user_id, msg_id = cq.from_user.id, _parse_cb(cq)
        if (user_id, msg_id) not in _pending:
            await cq.answer("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.", show_alert=True)
            return
        set_state(user_id, UserState.AWAITING_FILE_VIDEO_TITLE, msg_id=msg_id, prompt=cq.message)
        await cq.message.edit_text(
            "🏷 ابعت العنوان اللي عايزه لهذا الفيديو.\n"
            "هيتكتب في بيانات الفيديو، ولو مفعّل «العنوان على الشاشة» في إعدادات الفيديو "
            "هيظهر كمان على الصورة أول ثواني.",
            reply_markup=cancel_kb(),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rename:vtitleclear:\d+$"))
    async def rename_vtitleclear_cb(client: Client, cq: CallbackQuery) -> None:
        def mutate(p: PendingFile, user) -> None:
            p.video_title = ""  # explicit "no title", beats the default template
        await _change(cq, mutate)


# --------------------------------------------------------------------- #
# Video edit step
# --------------------------------------------------------------------- #
async def _edit_video_step(
    status: Message,
    file_path: str,
    new_name: str,
    wm: Optional["ve.WatermarkSpec"],
    title: Optional[str],
    title_on_screen: bool,
) -> Tuple[str, str, list]:
    """
    Apply the watermark / title. Best-effort like the PDF steps: on any
    failure the original video is still sent, with a warning note.
    Returns (file_path, new_name, notes).
    """
    notes: list = []
    needs_encode = wm is not None or (bool(title) and title_on_screen)
    queued = needs_encode and ve.encode_semaphore().locked()
    try:
        await status.edit_text(
            ui.screen("⏳", "في قائمة معالجة الفيديو", "دورك جاي حالًا 💕") if queued else ui.screen("🎬", "جارٍ معالجة الفيديو", "هتتحسن الجودة والتفاصيل ✨")
        )
    except Exception:
        pass
    try:
        result = await ve.edit_video(
            file_path,
            watermark=wm,
            title=title,
            title_on_screen=title_on_screen,
            progress=VideoProgress(status).update,
        )
    except ve.FFmpegMissing:
        logger.error("ffmpeg is not installed; sending the video unedited")
        notes.append("⚠️ ffmpeg مش متثبّت على السيرفر، اتبعت الفيديو من غير تعديل")
        return file_path, new_name, notes
    except Exception as exc:
        logger.exception("Video edit failed")
        notes.append(f"⚠️ تعذّر تعديل الفيديو، اتبعت من غير تعديل ({str(exc)[:120]})")
        return file_path, new_name, notes

    if result.output_path != file_path:
        try:
            os.remove(file_path)
        except OSError:
            logger.warning("Could not remove %s", file_path)
        file_path = result.output_path
        if result.reencoded:
            new_name = os.path.splitext(new_name)[0] + ".mp4"
    notes.extend(result.notes)
    return file_path, new_name, notes


def _sent_file_id(sent) -> Optional[str]:
    """Telegram file_id of the media in the message we just uploaded."""
    for attr in ("video", "audio", "document"):
        media = getattr(sent, attr, None)
        if media is not None:
            return media.file_id
    return None


# --------------------------------------------------------------------- #
# Download -> edit -> upload
# --------------------------------------------------------------------- #
async def _process_and_reupload(
    client: Client, trigger_message: Message, pending: PendingFile, new_name: str
) -> None:
    user_id = pending.message.from_user.id
    chat_id = trigger_message.chat.id
    sem = job_semaphore()

    status = await trigger_message.reply_text(
        ui.screen("⏳", "في قائمة الانتظار", "دورك جاي حالًا 💕") if sem.locked() else ui.screen("📥", "جارٍ بدء التحميل", "بجهّز ملفك...")
    )

    # The display name can be 200 Arabic characters (= 400 bytes), more than a
    # filesystem allows, so the local copy gets a short name with the same extension.
    local_ext = file_extension(pending.file_name) or file_extension(new_name)
    download_path = os.path.join(
        DOWNLOAD_DIR, f"{user_id}_{int(time.time())}_{pending.message.id}{local_ext}"
    )
    file_path: Optional[str] = None
    auto_thumb: Optional[str] = None
    custom_cover = pending.custom_cover_path

    try:
        async with sem:
            # ---------------- download ----------------
            try:
                dl_progress = Progress(status, action="📥 جارٍ التحميل").update
                file_path = await with_flood_retry(
                    lambda: fast_download(
                        client, pending.message, download_path, progress=dl_progress
                    )
                )
            except Exception as exc:
                logger.exception("Download failed for user %s", user_id)
                await db.log_event(user_id, "file_fail", f"فشل التحميل: {pending.file_name}")
                await status.edit_text(ui.error(exc, "حصلت مشكلة في التحميل، ابعت الملف تاني 💕"))
                return

            try:
                verify_download(file_path, pending.file_size)
            except Exception as exc:
                logger.error("Download check failed for user %s: %s", user_id, exc)
                await status.edit_text(ui.error(exc, "حصلت مشكلة في التحميل، ابعت الملف تاني 💕"))
                return

            user = await db.get_user(user_id)

            # ---------------- PDF edits ----------------
            notes = []
            is_pdf = file_extension(file_path).lower() == ".pdf" or pending.is_pdf
            if is_pdf:
                cover_path, cover_mode = _resolve_cover(pending, user)
                patterns = user.watermark_patterns if user else None
                if patterns or cover_path:
                    loop = asyncio.get_running_loop()
                    wm_progress = WatermarkProgress(status, loop)

                    async def on_stage(stage: str) -> None:
                        text = (
                            ui.screen("🧽", "جارٍ تعديل العلامة المائية", "ثواني وتخلص...")
                            if stage == "watermark"
                            else ui.screen("🖼", "جارٍ وضع الصورة", "بحط الصورة في أول صفحة...")
                        )
                        try:
                            await status.edit_text(text)
                        except Exception:
                            pass

                    result = await process_pdf(
                        file_path,
                        watermark_patterns=patterns,
                        cover_path=cover_path,
                        cover_mode=cover_mode,
                        watermark_progress=wm_progress.report,
                        on_stage=on_stage,
                    )
                    if result.watermarks_removed:
                        notes.append(f"🧽 اتعدّلت {result.watermarks_removed} علامة مائية")
                    if result.cover_added:
                        notes.append("🖼 أُضيفت الصورة في أول صفحة")
                    notes.extend(f"⚠️ {w}" for w in result.warnings)

            # ---------------- PDF quality / size optimisation ----------------
            if is_pdf and pending.optimize:
                loop = asyncio.get_running_loop()
                opt_path = file_path + ".opt"
                try:
                    try:
                        await status.edit_text(ui.screen("✨", "جارٍ تحسين جودة الـ PDF", "بصغّر الحجم وأحافظ على الجودة..."))
                    except Exception:
                        pass
                    res = await loop.run_in_executor(
                        None, optimize_pdf, file_path, opt_path, DEFAULT_LEVEL,
                        WatermarkProgress(status, loop, action="✨ جارٍ تحسين الـ PDF").report,
                    )
                    if res.improved:
                        os.replace(opt_path, file_path)
                        notes.append(
                            f"✨ اتحسّن الـ PDF: {human_bytes(res.before)} ← {human_bytes(res.after)} "
                            f"(وفّرنا {res.saved_percent:.0f}%)"
                        )
                    else:
                        notes.append("ℹ️ الـ PDF ده متحسّن بالفعل، اتبعت زي ما هو")
                except Exception as exc:
                    logger.exception("PDF optimisation failed")
                    notes.append(f"⚠️ تعذّر تحسين الـ PDF، اتبعت من غير تحسين ({str(exc)[:100]})")
                finally:
                    if os.path.exists(opt_path):
                        try:
                            os.remove(opt_path)
                        except OSError:
                            pass

            # ---------------- video edits (watermark / title) ----------------
            if pending.is_video:
                vs = await db.get_video_settings(user_id)
                wm, title = _effective_video(pending, vs, new_name)
                if wm or title:
                    file_path, new_name, video_notes = await _edit_video_step(
                        status, file_path, new_name, wm, title, vs.title_overlay
                    )
                    notes.extend(video_notes)

            # ---------------- thumbnail / caption ----------------
            await status.edit_text(ui.screen("📤", "جارٍ تجهيز الرفع", "لحظات ويوصلك ملفك..."))
            thumb_path = user.thumbnail_path if user and _has_file(user.thumbnail_path) else None
            if thumb_path is None and is_pdf:
                candidate = os.path.join(THUMB_DIR, f"auto_{user_id}_{pending.message.id}.jpg")
                ok = await asyncio.get_running_loop().run_in_executor(
                    None, render_first_page_thumbnail, file_path, candidate
                )
                if ok:
                    auto_thumb = thumb_path = candidate

            actual_size = os.path.getsize(file_path)
            duration = None
            width = height = None
            if pending.media_type == "video":
                # Real size/length from ffmpeg: a video uploaded without them can
                # show stretched or as 0:00 in some Telegram clients.
                vinfo = await ve.probe_safe(file_path)
                if vinfo:
                    width, height = vinfo.width, vinfo.height
                    duration = int(round(vinfo.duration)) or None
                if thumb_path is None:
                    candidate = os.path.join(THUMB_DIR, f"auto_{user_id}_{pending.message.id}.jpg")
                    if await ve.make_thumbnail(file_path, candidate, at=min(1.0, (duration or 2) / 2)):
                        auto_thumb = thumb_path = candidate
            if duration is None and pending.media_type in ("video", "audio"):
                duration = await asyncio.get_running_loop().run_in_executor(None, get_media_duration, file_path)
            caption = render_caption(
                user.custom_caption if user else None,
                filename=new_name,
                filesize=actual_size,
                duration=duration,
                link=user.custom_link if user else None,
            )

            # ---------------- upload (verified) ----------------
            try:
                sent = await send_verified(
                    client,
                    chat_id,
                    pending.media_type,
                    file_path,
                    new_name,
                    caption,
                    thumb=thumb_path,
                    duration=duration,
                    progress=Progress(status, action="📤 جارٍ الرفع").update,
                    width=width,
                    height=height,
                )
            except Exception as exc:
                logger.exception("Upload failed for user %s", user_id)
                await db.log_event(user_id, "file_fail", f"فشل الرفع: {new_name} — {str(exc)[:100]}")
                await status.edit_text(ui.error(exc, "حصلت مشكلة في الرفع، جرّب تاني 💕"))
                return

            await db.incr_files(user_id)
            # Keep a record (with the Telegram file_id) so the owner can browse
            # and download every file the bot edited.
            try:
                await db.log_file(
                    user_id, pending.file_name, new_name, pending.media_type, actual_size,
                    _sent_file_id(sent), "\n".join(notes) or None,
                )
                await db.log_event(user_id, "file_done", f"{pending.file_name} ← {new_name}")
            except Exception:
                logger.exception("Could not log the edited file of user %s", user_id)
            await status.edit_text(ui.success("\n".join(notes)))
    except Exception as exc:
        logger.exception("Processing failed for user %s", user_id)
        await db.log_event(user_id, "file_fail", f"خطأ أثناء المعالجة: {str(exc)[:120]}")
        try:
            await status.edit_text(ui.error(exc))
        except Exception:
            pass
    finally:
        for path in (file_path, auto_thumb, custom_cover):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    logger.warning("Could not remove temp file %s", path)
