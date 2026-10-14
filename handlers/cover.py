"""
handlers/cover.py
===================
"Image on the first page of the PDF" — the user's saved default image.

Flow (all buttons):
    [📄 صورة أول صفحة] -> set / show / toggle auto-apply / mode / delete

Once an image is saved and auto-apply is on, every PDF the user sends is
edited before it is re-uploaded (see utils/pdf_cover.py for the two modes).
A one-off image for a single file is chosen from that file's own buttons
(handlers/rename.py), and a photo sent out of the blue can also be turned
into the saved cover through the "use for first page" button.

`save_cover_from_message` and `process_cover_photo` are plain top-level
functions so handlers/common.py and handlers/rename.py can reuse them.
"""

import asyncio
import logging
import os

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from config import COVER_DIR
from database import db
from handlers.thumbnail import process_thumbnail, thumb_source_path, thumb_source_stem
from utils.keyboards import cancel_kb, cover_menu_kb, cover_saved_kb
from config import THUMB_DIR
from utils.pdf_cover import (
    MODE_LABELS,
    find_image,
    next_mode,
    normalize_mode,
    prepare_cover_image,
    store_image_copy,
)
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)


QUALITY_TIP = (
    "💡 **للجودة الكاملة أرسلها كـ «ملف» (File)** وليس كصورة — تيليجرام يضغط "
    "الصور المرسلة كصور ويصغّرها، أما الملف فيصل بأصله."
)


def user_cover_stem(user_id: int) -> str:
    return os.path.join(COVER_DIR, f"{user_id}")


async def save_cover_from_message(client: Client, message: Message, dest_stem: str) -> str:
    """Download the photo/image-document in `message` and store it under
    `dest_stem` with no quality loss (see utils.pdf_cover.prepare_cover_image).
    Returns the final path."""
    raw_path = await client.download_media(message, file_name=dest_stem + ".raw")
    if not raw_path:
        raise RuntimeError("تعذّر تحميل الصورة")
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, prepare_cover_image, raw_path, dest_stem
        )
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)


async def _cover_to_thumb(user_id: int, cover_path: str) -> None:
    """Use the (high-res) cover image as the user's thumbnail too."""
    thumb_path = os.path.join(THUMB_DIR, f"{user_id}.jpg")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, process_thumbnail, cover_path, thumb_path)
    await loop.run_in_executor(None, store_image_copy, cover_path, thumb_source_stem(user_id))
    await db.set_user_thumbnail(user_id, thumb_path)


def _has_cover(user) -> bool:
    return bool(user and user.cover_path and os.path.exists(user.cover_path))


def _menu_text(user) -> str:
    has = _has_cover(user)
    enabled = bool(user.cover_enabled) if user else True
    mode = normalize_mode(user.cover_mode if user else None)
    return (
        "📄 **صورة أول صفحة في ملفات الـ PDF**\n\n"
        "اختر صورة، وسيضعها البوت تلقائيًا في أول صفحة من أي PDF ترسله.\n\n"
        f"**الصورة:** {'✅ محفوظة' if has else '❌ لا توجد'}\n"
        f"**التطبيق التلقائي:** {'🟢 مفعّل' if enabled else '⚪️ متوقف'}\n"
        f"**الوضع:** {MODE_LABELS[mode]}\n\n"
        "💡 يمكنك أيضًا اختيار صورة مختلفة لملف واحد فقط من أزرار الملف نفسه."
    )


async def _show_menu(message: Message, user_id: int, edit: bool = True) -> None:
    user = await db.get_user(user_id)
    kb = cover_menu_kb(
        bool(user.cover_enabled) if user else True,
        MODE_LABELS[normalize_mode(user.cover_mode if user else None)],
    )
    if edit:
        await message.edit_text(_menu_text(user), reply_markup=kb)
    else:
        await message.reply_text(_menu_text(user), reply_markup=kb)


async def process_cover_photo(client: Client, message: Message) -> None:
    """Called by handlers/common.py when state == AWAITING_COVER_PHOTO."""
    user_id = message.from_user.id
    clear_state(user_id)
    status = await message.reply_text("⏳ جارٍ حفظ الصورة...")
    try:
        path = await save_cover_from_message(client, message, user_cover_stem(user_id))
        await db.set_user_cover(user_id, path)
        await db.set_user_cover_enabled(user_id, True)
        await status.delete()
        await _show_menu(message, user_id, edit=False)
        await message.reply_text(
            "✅ تم حفظ الصورة. ستُوضع في أول صفحة من كل PDF ترسله.\n\n"
            "تحب نفس الصورة تكون الصورة المصغرة كمان؟",
            reply_markup=cover_saved_kb(),
        )
    except Exception as exc:
        logger.exception("Failed to save cover for user %s", user_id)
        await status.edit_text(f"❌ فشل حفظ الصورة: {exc}")


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:cover$"))
    async def menu_cover_cb(client: Client, cq: CallbackQuery) -> None:
        await _show_menu(cq.message, cq.from_user.id)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^cover:set$"))
    async def cover_set_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_COVER_PHOTO)
        await cq.message.edit_text(
            "📄 أرسل الآن الصورة التي تريد وضعها في أول صفحة من ملفات الـ PDF.\n\n"
            + QUALITY_TIP,
            reply_markup=cancel_kb(),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^cover:show$"))
    async def cover_show_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        if not _has_cover(user):
            await cq.answer("لا توجد صورة محفوظة بعد.", show_alert=True)
            return
        await cq.message.reply_photo(user.cover_path, caption="📄 هذه صورة أول صفحة الحالية.")
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^cover:toggle$"))
    async def cover_toggle_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        if not _has_cover(user):
            await cq.answer("اضبط صورة أولًا.", show_alert=True)
            return
        await db.set_user_cover_enabled(cq.from_user.id, not user.cover_enabled)
        await _show_menu(cq.message, cq.from_user.id)
        await cq.answer("تم التبديل ✅")

    @app.on_callback_query(filters.regex(r"^cover:mode$"))
    async def cover_mode_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        await db.set_user_cover_mode(
            cq.from_user.id, next_mode(user.cover_mode if user else None)
        )
        await _show_menu(cq.message, cq.from_user.id)
        await cq.answer("تم تغيير الوضع ✅")

    @app.on_callback_query(filters.regex(r"^cover:del$"))
    async def cover_del_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        if not _has_cover(user):
            await cq.answer("لا توجد صورة لحذفها.", show_alert=True)
            return
        try:
            os.remove(user.cover_path)
        except OSError:
            pass
        await db.delete_user_cover(cq.from_user.id)
        await _show_menu(cq.message, cq.from_user.id)
        await cq.answer("🗑 تم الحذف")

    @app.on_callback_query(filters.regex(r"^thumb:confirm_cover$"))
    async def photo_as_cover_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        entry = get_state(user_id)
        source = entry.data.get("message")
        clear_state(user_id)
        if entry.state != UserState.AWAITING_THUMB_CONFIRM or source is None:
            await cq.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
            return
        await cq.message.edit_text("⏳ جارٍ حفظ الصورة...")
        try:
            path = await save_cover_from_message(client, source, user_cover_stem(user_id))
            await db.set_user_cover(user_id, path)
            await db.set_user_cover_enabled(user_id, True)
            await _show_menu(cq.message, user_id)
        except Exception as exc:
            logger.exception("Failed to save cover for user %s", user_id)
            await cq.message.edit_text(f"❌ فشل حفظ الصورة: {exc}")
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^thumb:confirm_both$"))
    async def photo_as_both_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        entry = get_state(user_id)
        source = entry.data.get("message")
        clear_state(user_id)
        if entry.state != UserState.AWAITING_THUMB_CONFIRM or source is None:
            await cq.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
            return
        await cq.message.edit_text("⏳ جارٍ حفظ الصورة...")
        try:
            cover_path = await save_cover_from_message(client, source, user_cover_stem(user_id))
            await _cover_to_thumb(user_id, cover_path)
            await db.set_user_cover(user_id, cover_path)
            await db.set_user_cover_enabled(user_id, True)
            await _show_menu(cq.message, user_id)
            await cq.message.reply_text("✅ الصورة اتحفظت كصورة مصغرة وكصورة أول صفحة PDF.")
        except Exception as exc:
            logger.exception("Failed to save both for user %s", user_id)
            await cq.message.edit_text(f"❌ فشل الحفظ: {exc}")
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^thumb:to_cover$"))
    async def thumb_to_cover_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        src = thumb_source_path(user_id)
        if not src:
            await cq.answer("ابعت الصورة من جديد من «تعيين صورة جديدة».", show_alert=True)
            return
        dest = await asyncio.get_running_loop().run_in_executor(
            None, store_image_copy, src, user_cover_stem(user_id)
        )
        await db.set_user_cover(user_id, dest)
        await db.set_user_cover_enabled(user_id, True)
        await _show_menu(cq.message, user_id)
        await cq.answer("✅ اتحطت كصورة أول صفحة")

    @app.on_callback_query(filters.regex(r"^cover:to_thumb$"))
    async def cover_to_thumb_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        user = await db.get_user(user_id)
        if not _has_cover(user):
            await cq.answer("مفيش صورة محفوظة.", show_alert=True)
            return
        await _cover_to_thumb(user_id, user.cover_path)
        await cq.message.edit_text("✅ الصورة بقت هي الصورة المصغرة كمان.", reply_markup=cover_saved_kb())
        await cq.answer()
