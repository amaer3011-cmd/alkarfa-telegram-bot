"""
handlers/studio.py
====================
🧰 استوديو PDF — a small document workshop inside Telegram:

    merge · split · extract pages · reorder · rotate · compress / improve · images -> PDF

Everything is button-driven like the rest of the bot. A flow keeps its progress in
the shared state store (utils/state.py):

    STUDIO_COLLECT   several inputs are being collected (merge: PDFs, images: pictures)
    STUDIO_WAIT_PDF  the tool is open and waits for the single PDF it works on
    STUDIO_WAIT_TEXT the PDF is downloaded; waiting for a button / the page numbers

Files downloaded for a flow are listed in data["_tmp"], so clearing or replacing the
state (cancel, /start, a new tool, expiry) deletes them. A running job takes
ownership of its files and removes them itself.

Every output is verified (utils/pdf_tools -> verify_pdf) and every upload is
size-checked (utils/sender.send_verified) before the user ever gets it.
"""

import asyncio
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from config import DOWNLOAD_DIR, MAX_FILE_SIZE, PENDING_TTL, THUMB_DIR
from database import db
from utils import pdf_tools as T
from utils.fast_transfer import fast_download
from utils.helpers import file_extension, human_bytes, render_caption, sanitize_filename
from utils.integrity import IntegrityError, verify_download
from utils import ui
from utils.keyboards import (
    studio_back_kb,
    studio_cancel_kb,
    studio_collect_kb,
    studio_levels_kb,
    studio_menu_kb,
    studio_reorder_kb,
    studio_rotate_kb,
    studio_rotate_pages_kb,
    studio_split_kb,
)
from utils.pdf_cover import render_first_page_thumbnail
from utils.pdf_optimize import DEFAULT_LEVEL, LEVELS, optimize_pdf
from utils.progress import Progress, WatermarkProgress
from utils.sender import job_semaphore, send_verified, spawn, with_flood_retry
from utils.state import UserState, all_states, clear_state, get_state, set_state

logger = logging.getLogger(__name__)

# ---- limits ---------------------------------------------------------------
MAX_MERGE_FILES = 30
MAX_IMAGES = 100
MAX_COLLECT_BYTES = 1536 * 1024 * 1024   # everything collected for one job
SETTLE_SECONDS = 1.6                     # quiet time before the panel is re-posted at the bottom

_SINGLE_TOOLS = ("split", "extract", "reorder", "rotate", "compress")
_COLLECT_TOOLS = ("merge", "images")

HOME_TEXT = (
    ui.header("🧰", "استوديو PDF")
    + "\nأدوات تجهيز المستندات جوه تيليجرام. اختار الأداة:\n\n"
    "🔗 **دمج** — اجمع كذا PDF في ملف واحد\n"
    "✂️ **تقسيم** — قسّم ملف لأجزاء\n"
    "📑 **استخراج صفحات** — خد صفحات معينة بس\n"
    "🔀 **ترتيب الصفحات** — غيّر ترتيبها\n"
    "🔄 **تدوير** — لف الصفحات 90° / 180°\n"
    "🗜 **ضغط وتحسين** — أصغر حجم بأعلى جودة\n"
    "🖼 **صور إلى PDF** — حوّل صورك لملف واحد"
)

PAGES_HELP = (
    "اكتب أرقام الصفحات، مثال:\n"
    "`1-3,7,10-12`\n"
    "`5-end` ← من صفحة 5 لآخر صفحة"
)


# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #
def _stem(name: str) -> str:
    stem = os.path.splitext(name or "file")[0].strip() or "file"
    return sanitize_filename(stem)[:120] or "file"


def _out_path(uid: int, tag: str, ext: str = ".pdf") -> str:
    return os.path.join(DOWNLOAD_DIR, f"studio_out_{uid}_{int(time.time() * 1000)}_{tag}{ext}")


def _in_path(uid: int, msg_id: int, ext: str) -> str:
    return os.path.join(DOWNLOAD_DIR, f"studio_{uid}_{int(time.time() * 1000)}_{msg_id}{ext}")


def _remove(*paths: Optional[str]) -> None:
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove %s", path)


def _is_pdf_doc(doc) -> bool:
    name = (doc.file_name or "").lower()
    return (doc.mime_type or "").lower() == "application/pdf" or name.endswith(".pdf")


def _is_image_doc(doc) -> bool:
    return (doc.mime_type or "").lower().startswith("image/")


def _purge_stale() -> None:
    """Forget studio flows nobody touched for PENDING_TTL (deletes their temp files)."""
    now = time.time()
    for uid, entry in list(all_states().items()):
        if entry.state in (UserState.STUDIO_COLLECT, UserState.STUDIO_WAIT_PDF, UserState.STUDIO_WAIT_TEXT):
            if now - entry.data.get("_created", now) > PENDING_TTL:
                clear_state(uid)


def _cleanup_leftovers() -> None:
    """Temp files of a previous run (the in-memory flows are gone after a restart)."""
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            if name.startswith("studio_"):
                _remove(os.path.join(DOWNLOAD_DIR, name))
    except OSError:
        pass


def _entry(uid: int, *states: UserState):
    entry = get_state(uid)
    return entry if entry.state in states else None


async def _stale(cq: CallbackQuery) -> None:
    await cq.answer("⚠️ انتهت الجلسة. افتح «🧰 استوديو PDF» من جديد.", show_alert=True)


def _set(uid: int, state: UserState, data: dict) -> dict:
    """set_state that keeps the same `_tmp` list and creation time; returns the new data dict."""
    keep = {k: v for k, v in data.items()}
    keep.setdefault("_created", time.time())
    set_state(uid, state, **keep)
    return get_state(uid).data


# --------------------------------------------------------------------- #
# Collect panel (merge / images)
# --------------------------------------------------------------------- #
def _items_sorted(d: dict) -> list:
    return sorted(d["items"], key=lambda it: it["message"].id)


def _panel_text(d: dict) -> str:
    tool = d["tool"]
    items = _items_sorted(d)
    total = sum(it["size"] for it in items)
    if tool == "merge":
        head = (
            "🔗 **دمج ملفات PDF**\n\n"
            f"ابعت الملفات **بالترتيب** اللي عايزه (من 2 لـ {MAX_MERGE_FILES} ملف) وبعدها اضغط «دمج».\n"
        )
        unit = "ملف"
    else:
        head = (
            "🖼 **تحويل صور إلى PDF**\n\n"
            f"ابعت الصور **بالترتيب** اللي عايزه (لحد {MAX_IMAGES} صورة) وبعدها اضغط «تحويل».\n"
            "💡 ابعتها «كملف» (Document) عشان تفضل بأعلى جودة.\n"
        )
        unit = "صورة"
    text = head + f"\n📚 استلمت: **{len(items)}** {unit}"
    if total:
        text += f" ({human_bytes(total)})"
    if items:
        lines = [f"{i}. {it['name']}" for i, it in enumerate(items[:12], start=1)]
        if len(items) > 12:
            lines.append(f"… و{len(items) - 12} تانيين")
        text += "\n\n" + "\n".join(lines)
    return text[:4000]


def _panel_kb(d: dict):
    layout = T.LAYOUT_LABELS[d.get("layout", T.LAYOUT_FIT)]
    return studio_collect_kb(len(d["items"]), d["tool"], layout)


async def _edit_panel(d: dict) -> None:
    panel: Optional[Message] = d.get("panel")
    if panel is None:
        return
    try:
        await panel.edit_text(_panel_text(d), reply_markup=_panel_kb(d))
    except Exception:
        pass  # identical text / message gone: the next re-post fixes it


async def _settle(client: Client, uid: int, chat_id: int, version: int) -> None:
    """After a quiet moment, move the panel to the bottom of the chat (below the new files)."""
    await asyncio.sleep(SETTLE_SECONDS)
    entry = _entry(uid, UserState.STUDIO_COLLECT)
    if entry is None or entry.data.get("_ver") != version:
        return
    d = entry.data
    old: Optional[Message] = d.get("panel")
    try:
        new = await client.send_message(chat_id, _panel_text(d), reply_markup=_panel_kb(d))
    except Exception:
        logger.exception("Could not re-post the studio panel")
        await _edit_panel(d)
        return
    # The user may have pressed a button while we were sending.
    entry = _entry(uid, UserState.STUDIO_COLLECT)
    if entry is None or entry.data is not d:
        try:
            await new.delete()
        except Exception:
            pass
        return
    d["panel"] = new
    if old is not None:
        try:
            await old.delete()
        except Exception:
            pass


def _schedule_settle(client: Client, uid: int, chat_id: int, d: dict) -> None:
    d["_ver"] = d.get("_ver", 0) + 1
    spawn(_settle(client, uid, chat_id, d["_ver"]))


# --------------------------------------------------------------------- #
# Incoming files / images (called from handlers/rename.py and handlers/common.py)
# --------------------------------------------------------------------- #
async def on_document(client: Client, message: Message) -> bool:
    """
    A document/video/audio arrived. True if the studio consumed it (the user is inside a
    studio flow), False to let the normal rename flow handle it.
    """
    uid = message.from_user.id
    entry = get_state(uid)
    if entry.state not in (UserState.STUDIO_COLLECT, UserState.STUDIO_WAIT_PDF, UserState.STUDIO_WAIT_TEXT):
        return False
    _purge_stale()
    d = entry.data
    tool = d.get("tool")
    doc = message.document

    if entry.state == UserState.STUDIO_COLLECT:
        if tool == "merge":
            if doc is None or not _is_pdf_doc(doc):
                await message.reply_text("⚠️ في الدمج ابعت ملفات PDF بس.")
                return True
        else:  # images
            if doc is None or not _is_image_doc(doc):
                await message.reply_text("⚠️ ابعت صور (أو صور «كملف»).")
                return True
        await _collect_add(client, message, entry.data, doc.file_name or f"file_{message.id}", doc.file_size or 0)
        return True

    # single-PDF tools
    if doc is None or not _is_pdf_doc(doc):
        await message.reply_text("⚠️ ابعت ملف PDF.", reply_markup=studio_cancel_kb())
        return True
    await _single_received(client, message, tool)
    return True


async def on_photo(client: Client, message: Message) -> None:
    """A photo arrived while the user is in a studio state (route_image in common.py)."""
    uid = message.from_user.id
    entry = get_state(uid)
    if entry.state == UserState.STUDIO_COLLECT and entry.data.get("tool") == "images":
        photo = message.photo
        await _collect_add(
            client, message, entry.data, f"photo_{message.id}.jpg", getattr(photo, "file_size", 0) or 0
        )
        return
    if entry.state == UserState.STUDIO_COLLECT:
        await message.reply_text("⚠️ في الدمج ابعت ملفات PDF بس.")
    else:
        await message.reply_text("⚠️ ابعت ملف PDF.", reply_markup=studio_cancel_kb())


async def _collect_add(client: Client, message: Message, d: dict, name: str, size: int) -> None:
    tool = d["tool"]
    limit = MAX_MERGE_FILES if tool == "merge" else MAX_IMAGES
    if len(d["items"]) >= limit:
        await message.reply_text(f"⚠️ وصلت للحد الأقصى ({limit}). اضغط «✅» عشان نكمّل.")
        return
    if size and size > MAX_FILE_SIZE:
        await message.reply_text(f"⚠️ «{name}» كبير جدًا ({human_bytes(size)}).")
        return
    if sum(it["size"] for it in d["items"]) + size > MAX_COLLECT_BYTES:
        await message.reply_text(f"⚠️ الحجم الكلي عدّى {human_bytes(MAX_COLLECT_BYTES)}. اضغط «✅» بالملفات اللي اتبعتت.")
        return
    if any(it["message"].id == message.id for it in d["items"]):
        return
    d["items"].append({"message": message, "name": name, "size": size})
    _schedule_settle(client, message.from_user.id, message.chat.id, d)


async def process_stray_text(client: Client, message: Message) -> None:
    """Text while the studio waits for files or for a button."""
    entry = get_state(message.from_user.id)
    if entry.state == UserState.STUDIO_COLLECT:
        what = "ملفات PDF" if entry.data.get("tool") == "merge" else "صور"
        await message.reply_text(f"📎 ابعت {what}، وبعدها اضغط الزرار في اللوحة.", reply_markup=studio_cancel_kb())
    else:
        await message.reply_text("📎 ابعت ملف PDF.", reply_markup=studio_cancel_kb())


# --------------------------------------------------------------------- #
# Single-PDF tools: download, inspect, then ask for what the tool needs
# --------------------------------------------------------------------- #
async def _single_received(client: Client, message: Message, tool: str) -> None:
    uid = message.from_user.id
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await message.reply_text(f"⚠️ الملف كبير جدًا ({human_bytes(doc.file_size)}).")
        return

    path = _in_path(uid, message.id, ".pdf")
    tmp: List[str] = [path]  # a NEW list: replaces (and so deletes) whatever the previous file was
    d = _set(uid, UserState.STUDIO_WAIT_PDF, {"tool": tool, "_tmp": tmp})
    status = await message.reply_text(ui.screen("📥", "جارٍ التحميل", "ثواني ويكون جاهز..."))
    loop = asyncio.get_running_loop()
    try:
        async with job_semaphore():
            await with_flood_retry(
                lambda: fast_download(client, message, path, progress=Progress(status, action="📥 جارٍ التحميل").update)
            )
        verify_download(path, doc.file_size or 0)
        pages = await loop.run_in_executor(None, T.page_count, path)
    except T.ToolError as exc:
        await _fail_input(uid, tmp, status, f"❌ {exc}")
        return
    except Exception as exc:
        logger.exception("Studio download failed for user %s", uid)
        await _fail_input(uid, tmp, status, f"❌ فشل التحميل: {exc}")
        return

    # Cancelled / replaced while downloading?
    cur = _entry(uid, UserState.STUDIO_WAIT_PDF)
    if cur is None or cur.data.get("_tmp") is not tmp:
        _remove(path)
        return

    name = doc.file_name or f"file_{message.id}.pdf"
    base = {
        "tool": tool, "_tmp": tmp, "_created": d.get("_created", time.time()),
        "path": path, "name": name, "pages": pages, "size": os.path.getsize(path),
    }
    info = f"📄 `{name}`\n📃 {pages} صفحة • 📦 {human_bytes(base['size'])}\n\n"

    if tool == "compress":
        _set(uid, UserState.STUDIO_WAIT_TEXT, {**base, "step": "level"})
        await status.edit_text(info + "🗜 اختار مستوى التحسين:", reply_markup=studio_levels_kb())
    elif tool == "extract":
        _set(uid, UserState.STUDIO_WAIT_TEXT, {**base, "step": "text"})
        await status.edit_text(info + "📑 **استخراج صفحات**\n" + PAGES_HELP, reply_markup=studio_cancel_kb())
    elif tool == "reorder":
        _set(uid, UserState.STUDIO_WAIT_TEXT, {**base, "step": "text"})
        await status.edit_text(
            info + "🔀 **ترتيب الصفحات**\nاكتب الترتيب الجديد، مثال:\n`3,1,2,4-end`\n"
            "`5-1` ← من 5 نازل لـ 1\n"
            "الصفحات اللي ما تكتبهاش هتتحط في الآخر بترتيبها.",
            reply_markup=studio_reorder_kb(),
        )
    elif tool == "rotate":
        _set(uid, UserState.STUDIO_WAIT_TEXT, {**base, "step": "angle"})
        await status.edit_text(info + "🔄 اختار اتجاه التدوير:", reply_markup=studio_rotate_kb())
    else:  # split
        _set(uid, UserState.STUDIO_WAIT_TEXT, {**base, "step": "split_mode"})
        await status.edit_text(info + "✂️ **تقسيم الملف** — اختار الطريقة:", reply_markup=studio_split_kb())


async def _fail_input(uid: int, tmp: list, status: Message, text: str) -> None:
    """The file the user sent can't be used: drop it, stay in the tool so they can resend."""
    cur = get_state(uid)
    if cur.data.get("_tmp") is tmp:
        tool = cur.data.get("tool")
        _set(uid, UserState.STUDIO_WAIT_PDF, {"tool": tool, "_tmp": []})  # deletes the bad file
    else:
        for path in tmp:
            _remove(path)
    try:
        await status.edit_text(text + "\n\nابعت ملف PDF تاني أو اضغط إلغاء.", reply_markup=studio_cancel_kb())
    except Exception:
        pass


async def process_text(client: Client, message: Message) -> None:
    """state == STUDIO_WAIT_TEXT: page numbers / a number typed by the user."""
    uid = message.from_user.id
    entry = _entry(uid, UserState.STUDIO_WAIT_TEXT)
    if entry is None:
        return
    d = entry.data
    step, tool, pages = d.get("step"), d.get("tool"), d.get("pages", 0)
    text = (message.text or "").strip()

    if step not in ("text", "rot_pages"):
        await message.reply_text("👆 اختار من الأزرار اللي فوق.", reply_markup=studio_cancel_kb())
        return

    try:
        if tool == "extract":
            T.parse_pages(text, pages)
            params = {"spec": text}
        elif tool == "reorder":
            T.parse_pages(text, pages, allow_desc=True)
            params = {"spec": text}
        elif tool == "rotate":
            T.parse_pages(text, pages)
            params = {"angle": d["angle"], "spec": text}
        elif tool == "split":
            T.split_ranges_of(pages, d["mode"], text)
            params = {"mode": d["mode"], "value": text}
        else:
            return
    except T.ToolError as exc:
        await message.reply_text(f"⚠️ {exc}\n\nجرّب تاني:", reply_markup=studio_cancel_kb())
        return
    await _start_single_job(client, message, uid, tool, params)


async def _start_single_job(
    client: Client, trigger: Message, uid: int, tool: str, params: dict, edit: bool = False
) -> None:
    """
    Hand the downloaded PDF over to a background job (which owns and deletes it).
    `edit`: `trigger` is the bot's own prompt message (a button was pressed) and becomes the
    status message; otherwise it is the user's text and the status is a reply to it.
    """
    entry = get_state(uid)
    d = entry.data
    src, name = d["path"], d["name"]
    d["_tmp"].clear()      # the job owns the file now; clear_state must not delete it
    clear_state(uid)
    if edit:
        await trigger.edit_text(ui.screen("⚙️", "جارٍ المعالجة", "بشتغل على ملفك ✨"))
        status = trigger
    else:
        status = await trigger.reply_text(ui.screen("⚙️", "جارٍ المعالجة", "بشتغل على ملفك ✨"))
    spawn(_single_job(client, status, uid, trigger.chat.id, tool, src, name, params))


# --------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------- #
async def _deliver(
    client: Client, chat_id: int, uid: int, status: Message, path: str, name: str,
    orig_name: str, notes: Sequence[str], action: str = "📤 جارٍ الرفع",
) -> None:
    """Send one finished PDF with the user's thumbnail / caption / link, verified."""
    user = await db.get_user(uid)
    thumb = user.thumbnail_path if user and user.thumbnail_path and os.path.exists(user.thumbnail_path) else None
    auto_thumb = None
    if thumb is None:
        candidate = os.path.join(THUMB_DIR, f"auto_studio_{uid}_{int(time.time() * 1000)}.jpg")
        if await asyncio.get_running_loop().run_in_executor(None, render_first_page_thumbnail, path, candidate):
            auto_thumb = thumb = candidate
    try:
        size = os.path.getsize(path)
        caption = render_caption(
            user.custom_caption if user else None,
            filename=name, filesize=size, link=user.custom_link if user else None,
        )
        sent = await send_verified(
            client, chat_id, "document", path, name, caption,
            thumb=thumb, progress=Progress(status, action=action).update,
        )
    finally:
        _remove(auto_thumb)
    await db.incr_files(uid)
    try:
        file_id = sent.document.file_id if sent is not None and sent.document else None
        await db.log_file(uid, orig_name, name, "document", size, file_id, "\n".join(notes) or None)
        await db.log_event(uid, "file_done", f"استوديو PDF: {orig_name} ← {name}")
    except Exception:
        logger.exception("Could not log the studio file of user %s", uid)


async def _finish(status: Message, text: str) -> None:
    try:
        await status.edit_text(text, reply_markup=studio_back_kb())
    except Exception:
        pass


async def _single_job(
    client: Client, status: Message, uid: int, chat_id: int, tool: str,
    src: str, name: str, params: dict,
) -> None:
    loop = asyncio.get_running_loop()
    stem = _stem(name)
    outputs: List[Tuple[str, str]] = []   # (path, display name)
    notes: List[str] = []
    try:
        async with job_semaphore():
            if tool == "extract":
                out = _out_path(uid, "x")
                kept, total = await loop.run_in_executor(None, T.extract_pages, src, params["spec"], out)
                outputs.append((out, f"{stem}_مستخرج.pdf"))
                notes.append(f"📑 {kept} صفحة من {total}")
            elif tool == "reorder":
                out = _out_path(uid, "r")
                if params.get("reverse"):
                    n = await loop.run_in_executor(None, T.reverse_pages, src, out)
                    notes.append(f"🔃 اتعكس ترتيب {n} صفحة")
                else:
                    n, _total, appended = await loop.run_in_executor(None, T.reorder_pages, src, params["spec"], out)
                    notes.append(f"🔀 اتغيّر ترتيب {n} صفحة")
                    if appended:
                        shown = ", ".join(map(str, appended[:15])) + (" …" if len(appended) > 15 else "")
                        notes.append(f"ℹ️ الصفحات اللي ما اتذكرتش اتحطت في الآخر: {shown}")
                outputs.append((out, f"{stem}_مرتّب.pdf"))
            elif tool == "rotate":
                out = _out_path(uid, "t")
                rotated, total = await loop.run_in_executor(None, T.rotate_pages, src, params["angle"], params.get("spec"), out)
                outputs.append((out, f"{stem}_مدوّر.pdf"))
                notes.append(f"🔄 اتدوّرت {rotated} صفحة من {total} ({params['angle']}°)")
            elif tool == "split":
                total = await loop.run_in_executor(None, T.page_count, src)
                groups = T.split_ranges_of(total, params["mode"], params.get("value"))
                each = params["mode"] == "each"

                def dst_for(i: int, pages: Sequence[int]) -> str:
                    return _out_path(uid, f"s{i}")

                paths = await loop.run_in_executor(None, T.split_pdf, src, groups, dst_for)
                for i, (path, pages) in enumerate(zip(paths, groups)):
                    label = f"ص{pages[0] + 1}" if each else f"جزء{i + 1}"
                    outputs.append((path, f"{stem}_{label}.pdf"))
                notes.append(f"✂️ اتقسّم لـ {len(paths)} ملف")
            elif tool == "compress":
                out = _out_path(uid, "c")
                level = LEVELS.get(params.get("level")) or LEVELS[DEFAULT_LEVEL]
                res = await loop.run_in_executor(
                    None, optimize_pdf, src, out, level.key,
                    WatermarkProgress(status, loop, action="🗜 جارٍ تحسين الـ PDF").report,
                )
                if not res.improved:
                    await _finish(
                        status,
                        "ℹ️ الملف ده متحسّن بالفعل (مفيش صور تتصغّر من غير ما الجودة تتأثر) "
                        f"فمفيش داعي لإعادة إرساله.\n📦 الحجم: {human_bytes(res.before)}",
                    )
                    return
                outputs.append((out, f"{stem}_مضغوط.pdf"))
                notes.append(
                    f"🗜 {human_bytes(res.before)} ← {human_bytes(res.after)} (وفّرنا {res.saved_percent:.0f}%) • {level.label}"
                )

            count = len(outputs)
            for i, (path, out_name) in enumerate(outputs, start=1):
                action = "📤 جارٍ الرفع" if count == 1 else f"📤 جارٍ رفع الملف {i}/{count}"
                await _deliver(client, chat_id, uid, status, path, out_name, name, notes, action)
        await _finish(status, ui.success("\n".join(notes)))
    except (T.ToolError, IntegrityError) as exc:
        await _finish(status, f"❌ {exc}")
    except Exception as exc:
        logger.exception("Studio job %s failed for user %s", tool, uid)
        await db.log_event(uid, "file_fail", f"استوديو PDF ({tool}): {str(exc)[:120]}")
        await _finish(status, ui.error(str(exc)[:200]))
    finally:
        _remove(src, *[p for p, _n in outputs])


async def _collect_job(
    client: Client, status: Message, uid: int, chat_id: int, tool: str, items: list, layout: str,
) -> None:
    loop = asyncio.get_running_loop()
    paths: List[str] = []
    out = _out_path(uid, "m" if tool == "merge" else "i")
    try:
        async with job_semaphore():
            count = len(items)
            for i, it in enumerate(items, start=1):
                msg: Message = it["message"]
                ext = ".pdf" if tool == "merge" else (file_extension(it["name"]) or ".jpg")
                path = _in_path(uid, msg.id, ext)
                paths.append(path)
                action = f"📥 جارٍ تحميل {i}/{count}"
                await with_flood_retry(
                    lambda m=msg, p=path: fast_download(client, m, p, progress=Progress(status, action=action).update)
                )
                verify_download(path, it["size"] if msg.document else 0)

            await status.edit_text("🔗 جارٍ الدمج..." if tool == "merge" else "🖼 جارٍ تحويل الصور...")
            names = [it["name"] for it in items]
            if tool == "merge":
                total = await loop.run_in_executor(None, T.merge_pdfs, paths, names, out)
                out_name = f"{_stem(names[0])}_مدمج.pdf"
                note = f"🔗 اتدمج {count} ملف ({total} صفحة)"
            else:
                total = await loop.run_in_executor(None, T.images_to_pdf, paths, out, layout)
                out_name = "صور_PDF.pdf"
                note = f"🖼 {total} صورة ← ملف PDF ({T.LAYOUT_LABELS[layout]})"
            await _deliver(client, chat_id, uid, status, out, out_name, names[0], [note])
        await _finish(status, ui.success(note + "\n\n💡 تقدر تبعت الملف للبوت تاني لو عايز تغيّر اسمه."))
    except (T.ToolError, IntegrityError) as exc:
        await _finish(status, f"❌ {exc}")
    except Exception as exc:
        logger.exception("Studio %s job failed for user %s", tool, uid)
        await db.log_event(uid, "file_fail", f"استوديو PDF ({tool}): {str(exc)[:120]}")
        await _finish(status, ui.error(str(exc)[:200]))
    finally:
        _remove(out, *paths)


# --------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------- #
_TOOL_PROMPTS: Dict[str, str] = {
    "split": "✂️ **تقسيم ملف**\n\nابعت ملف الـ PDF اللي عايز تقسّمه.",
    "extract": "📑 **استخراج صفحات**\n\nابعت ملف الـ PDF، وبعدها هتكتب أرقام الصفحات اللي عايزها.",
    "reorder": "🔀 **ترتيب الصفحات**\n\nابعت ملف الـ PDF، وبعدها هتكتب الترتيب الجديد.",
    "rotate": "🔄 **تدوير الصفحات**\n\nابعت ملف الـ PDF اللي عايز تدوّر صفحاته.",
    "compress": (
        "🗜 **ضغط وتحسين**\n\nابعت ملف الـ PDF.\n"
        "هيصغّر الصور الزايدة في الدقة ويضغطها بذكاء، والنصوص والرسومات تفضل حادة تمامًا."
    ),
}


def register(app: Client) -> None:
    _cleanup_leftovers()

    @app.on_callback_query(filters.regex(r"^studio:home$"))
    async def studio_home_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await cq.message.edit_text(HOME_TEXT, reply_markup=studio_menu_kb())
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^studio:cancel$"))
    async def studio_cancel_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await cq.message.edit_text(ui.cancelled(), reply_markup=studio_back_kb())
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^studio:tool:(merge|images|split|extract|reorder|rotate|compress)$"))
    async def studio_tool_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        tool = cq.data.rsplit(":", 1)[1]
        _purge_stale()
        if tool in _COLLECT_TOOLS:
            d = _set(uid, UserState.STUDIO_COLLECT, {
                "tool": tool, "items": [], "panel": cq.message, "layout": T.LAYOUT_FIT, "_tmp": [],
            })
            await cq.message.edit_text(_panel_text(d), reply_markup=_panel_kb(d))
        else:
            _set(uid, UserState.STUDIO_WAIT_PDF, {"tool": tool, "panel": cq.message, "_tmp": []})
            await cq.message.edit_text(_TOOL_PROMPTS[tool], reply_markup=studio_cancel_kb())
        await cq.answer()

    # ---- collecting ----
    @app.on_callback_query(filters.regex(r"^studio:undo$"))
    async def studio_undo_cb(client: Client, cq: CallbackQuery) -> None:
        entry = _entry(cq.from_user.id, UserState.STUDIO_COLLECT)
        if entry is None:
            await _stale(cq)
            return
        d = entry.data
        if not d["items"]:
            await cq.answer("مفيش حاجة تتشال.", show_alert=True)
            return
        d["items"].remove(max(d["items"], key=lambda it: it["message"].id))
        d["panel"] = cq.message
        await _edit_panel(d)
        await cq.answer("اتشال آخر ملف")

    @app.on_callback_query(filters.regex(r"^studio:layout$"))
    async def studio_layout_cb(client: Client, cq: CallbackQuery) -> None:
        entry = _entry(cq.from_user.id, UserState.STUDIO_COLLECT)
        if entry is None:
            await _stale(cq)
            return
        d = entry.data
        d["layout"] = T.LAYOUT_A4 if d.get("layout", T.LAYOUT_FIT) == T.LAYOUT_FIT else T.LAYOUT_FIT
        d["panel"] = cq.message
        await _edit_panel(d)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^studio:go$"))
    async def studio_go_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        entry = _entry(uid, UserState.STUDIO_COLLECT)
        if entry is None:
            await _stale(cq)
            return
        d = entry.data
        tool = d["tool"]
        items = _items_sorted(d)
        if tool == "merge" and len(items) < 2:
            await cq.answer("ابعت ملفين PDF على الأقل.", show_alert=True)
            return
        if not items:
            await cq.answer("ابعت صورة على الأقل.", show_alert=True)
            return
        layout = d.get("layout", T.LAYOUT_FIT)
        clear_state(uid)
        await cq.message.edit_text(ui.screen("⚙️", "جارٍ التجهيز", "لحظات..."))
        await cq.answer()
        spawn(_collect_job(client, cq.message, uid, cq.message.chat.id, tool, items, layout))

    # ---- single-PDF steps ----
    @app.on_callback_query(filters.regex(r"^studio:lvl:(high|balanced|small)$"))
    async def studio_level_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        entry = _entry(uid, UserState.STUDIO_WAIT_TEXT)
        if entry is None or entry.data.get("step") != "level":
            await _stale(cq)
            return
        level = cq.data.rsplit(":", 1)[1]
        await cq.answer()
        await _start_single_job(client, cq.message, uid, "compress", {"level": level}, edit=True)

    @app.on_callback_query(filters.regex(r"^studio:rot:(90|180|270)$"))
    async def studio_rot_cb(client: Client, cq: CallbackQuery) -> None:
        entry = _entry(cq.from_user.id, UserState.STUDIO_WAIT_TEXT)
        if entry is None or entry.data.get("step") != "angle":
            await _stale(cq)
            return
        angle = int(cq.data.rsplit(":", 1)[1])
        entry.data.update(step="rot_pages", angle=angle)
        await cq.message.edit_text(
            f"🔄 التدوير: **{angle}°**\n\nعايز تدوّر أنهي صفحات؟\n" + PAGES_HELP,
            reply_markup=studio_rotate_pages_kb(),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^studio:rotall$"))
    async def studio_rotall_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        entry = _entry(uid, UserState.STUDIO_WAIT_TEXT)
        if entry is None or entry.data.get("step") != "rot_pages":
            await _stale(cq)
            return
        params = {"angle": entry.data["angle"], "spec": None}
        await cq.answer()
        await _start_single_job(client, cq.message, uid, "rotate", params, edit=True)

    @app.on_callback_query(filters.regex(r"^studio:reverse$"))
    async def studio_reverse_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        entry = _entry(uid, UserState.STUDIO_WAIT_TEXT)
        if entry is None or entry.data.get("tool") != "reorder":
            await _stale(cq)
            return
        await cq.answer()
        await _start_single_job(client, cq.message, uid, "reorder", {"reverse": True}, edit=True)

    @app.on_callback_query(filters.regex(r"^studio:split:(every|parts|ranges|each)$"))
    async def studio_split_cb(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        entry = _entry(uid, UserState.STUDIO_WAIT_TEXT)
        if entry is None or entry.data.get("step") != "split_mode":
            await _stale(cq)
            return
        d = entry.data
        mode = cq.data.rsplit(":", 1)[1]
        if mode == "each":
            try:
                T.split_ranges_of(d["pages"], "each", None)
            except T.ToolError as exc:
                await cq.answer(str(exc), show_alert=True)
                return
            await cq.answer()
            await _start_single_job(client, cq.message, uid, "split", {"mode": "each", "value": None}, edit=True)
            return
        d.update(step="text", mode=mode)
        prompts = {
            "every": f"✂️ كل كام صفحة في ملف؟ اكتب رقم (الملف فيه {d['pages']} صفحة).\nمثال: `10`",
            "parts": f"🔢 تقسّمه لكام جزء متساوي؟ اكتب رقم (الملف فيه {d['pages']} صفحة).\nمثال: `3`",
            "ranges": (
                f"📑 اكتب النطاقات، كل نطاق هيبقى ملف لوحده (الملف فيه {d['pages']} صفحة).\n"
                "مثال: `1-5, 6-10, 11-end`"
            ),
        }
        await cq.message.edit_text(prompts[mode], reply_markup=studio_cancel_kb())
        await cq.answer()
