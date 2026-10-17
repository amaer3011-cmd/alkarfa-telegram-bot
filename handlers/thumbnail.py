"""
handlers/thumbnail.py
=======================
Custom persistent thumbnail management — fully button-driven.

Flow:
    [🖼 الصورة المصغرة] -> [➕ تعيين صورة جديدة] -> user sends a photo
        -> resize/convert with Pillow (max 320x320, aspect preserved, JPEG)
        -> path saved in DB against the user's id

If a user just sends a photo out of the blue (no button pressed), the
shared photo router in handlers/common.py calls `offer_thumb_confirmation`
so the feature stays reachable without hunting through menus.

`process_thumb_photo` and `offer_thumb_confirmation` are plain top-level
functions (not nested inside `register`) so handlers/common.py can import
and call them directly based on the user's current state.
"""

import asyncio
import logging
import os

from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from config import THUMB_DIR
from database import db
from utils import ui
from utils.keyboards import cancel_kb, confirm_thumb_kb, thumb_menu_kb, thumb_saved_kb
from utils.pdf_cover import find_image, prepare_cover_image
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)

_MAX_DIMENSION = 320


def _thumb_path_for(user_id: int) -> str:
    return os.path.join(THUMB_DIR, f"{user_id}.jpg")


def thumb_source_stem(user_id: int) -> str:
    return os.path.join(THUMB_DIR, f"{user_id}_src")


def thumb_source_path(user_id: int) -> "str | None":
    """The full-resolution original saved next to the 320px thumbnail."""
    return find_image(thumb_source_stem(user_id))


def process_thumbnail(source_path: str, dest_path: str) -> None:
    """
    Resize an image to fit within 320x320 while preserving aspect ratio,
    then save it as a JPEG. Blocking — always call via a thread executor.
    """
    with Image.open(source_path) as img:
        img = img.convert("RGB")
        img.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)
        img.save(dest_path, "JPEG", quality=90)


async def _save_photo_as_thumbnail(client: Client, source_message: Message, user_id: int) -> None:
    """Download `source_message`'s photo, process it, and persist as the
    user's thumbnail. Shared by both the menu flow and the auto-detect flow."""
    raw_path = os.path.join(THUMB_DIR, f"raw_{user_id}.tmp")
    final_path = _thumb_path_for(user_id)
    try:
        await client.download_media(source_message, file_name=raw_path)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, process_thumbnail, raw_path, final_path)
        # Keep a high-resolution copy so the same picture can also be used
        # as the PDF first-page image without being blurry.
        await loop.run_in_executor(
            None, prepare_cover_image, raw_path, thumb_source_stem(user_id)
        )
        await db.set_user_thumbnail(user_id, final_path)
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)


async def process_thumb_photo(client: Client, message: Message) -> None:
    """Called by handlers/common.py when state == AWAITING_THUMB_PHOTO."""
    user_id = message.from_user.id
    clear_state(user_id)
    status = await message.reply_text(ui.screen("⏳", "جارٍ الحفظ", "بجهّز صورتك المصغرة..."))
    try:
        await _save_photo_as_thumbnail(client, message, user_id)
        await status.edit_text(
            ui.screen("✅", "تم حفظ الصورة المصغرة", "تحب نفس الصورة تتحط كمان في أول صفحة الـ PDF؟"),
            reply_markup=thumb_saved_kb(),
        )
    except Exception as exc:
        logger.exception("Failed to set thumbnail for user %s", user_id)
        await status.edit_text(f"❌ فشل حفظ الصورة المصغرة: {exc}")


async def offer_thumb_confirmation(client: Client, message: Message) -> None:
    """Called by handlers/common.py when a photo arrives with no pending
    state at all — offers to use it as the user's thumbnail."""
    set_state(message.from_user.id, UserState.AWAITING_THUMB_CONFIRM, message=message)
    is_doc = bool(message.document)
    text = "📸 ماذا تريد أن تفعل بهذه الصورة؟"
    if not is_doc:
        text += (
            "\n\n💡 للجودة الكاملة أرسلها كـ «ملف» (File) وليس كصورة، لأن تيليجرام "
            "يضغط الصور المرسلة كصور ويصغّرها."
        )
    await message.reply_text(text, reply_markup=confirm_thumb_kb(as_file_option=is_doc))


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:thumb$"))
    async def menu_thumb_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(
            ui.screen("🖼", "الصورة المصغرة", "بتتحط تلقائيًا على كل ملف يوصلك.", "اختر من الأزرار بالأسفل 👇"),
            reply_markup=thumb_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^thumb:set$"))
    async def thumb_set_cb(client: Client, callback_query: CallbackQuery) -> None:
        set_state(callback_query.from_user.id, UserState.AWAITING_THUMB_PHOTO)
        await callback_query.message.edit_text(
            "📸 أرسل الآن الصورة التي تريد استخدامها كصورة مصغرة.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^thumb:show$"))
    async def thumb_show_cb(client: Client, callback_query: CallbackQuery) -> None:
        user = await db.get_user(callback_query.from_user.id)
        if not user or not user.thumbnail_path or not os.path.exists(user.thumbnail_path):
            await callback_query.answer("لا توجد صورة مصغرة محفوظة بعد.", show_alert=True)
            return
        await callback_query.message.reply_photo(
            user.thumbnail_path,
            caption="🖼 هذه هي صورتك المصغرة الحالية.",
            reply_markup=thumb_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^thumb:del$"))
    async def thumb_del_cb(client: Client, callback_query: CallbackQuery) -> None:
        user_id = callback_query.from_user.id
        user = await db.get_user(user_id)
        if not user or not user.thumbnail_path:
            await callback_query.answer("لا توجد صورة مصغرة لحذفها.", show_alert=True)
            return
        if os.path.exists(user.thumbnail_path):
            os.remove(user.thumbnail_path)
        await db.delete_user_thumbnail(user_id)
        await callback_query.message.edit_text(
            "🗑 تم حذف الصورة المصغرة.", reply_markup=thumb_menu_kb()
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^thumb:confirm_yes$"))
    async def thumb_confirm_yes_cb(client: Client, callback_query: CallbackQuery) -> None:
        user_id = callback_query.from_user.id
        entry = get_state(user_id)
        source_message = entry.data.get("message")
        clear_state(user_id)

        if entry.state != UserState.AWAITING_THUMB_CONFIRM or source_message is None:
            await callback_query.answer("انتهت صلاحية هذا الطلب.", show_alert=True)
            return

        await callback_query.message.edit_text(ui.screen("⏳", "جارٍ الحفظ", "بجهّز صورتك المصغرة..."))
        try:
            await _save_photo_as_thumbnail(client, source_message, user_id)
            await callback_query.message.edit_text(
                ui.screen("✅", "تم حفظ الصورة المصغرة", "تحب نفس الصورة تتحط كمان في أول صفحة الـ PDF؟"),
                reply_markup=thumb_saved_kb(),
            )
        except Exception as exc:
            logger.exception("Failed to set thumbnail for user %s", user_id)
            await callback_query.message.edit_text(f"❌ فشل حفظ الصورة المصغرة: {exc}")
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^thumb:confirm_no$"))
    async def thumb_confirm_no_cb(client: Client, callback_query: CallbackQuery) -> None:
        clear_state(callback_query.from_user.id)
        await callback_query.message.edit_text("👍 حسنًا، لم يتم تغيير شيء.")
        await callback_query.answer()
