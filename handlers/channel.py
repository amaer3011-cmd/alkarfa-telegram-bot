"""
handlers/channel.py
=====================
Channel Auto-Formatter Mode — admin-only, fully button-driven.

An admin binds the bot to a channel (the bot must already be a channel
admin with "Post messages" and "Delete messages" permissions). Once
bound, every document posted to the channel is automatically:
    1. Downloaded
    2. Re-captioned using the channel's caption template
    3. Re-thumbnailed using the channel's default thumbnail
    4. Re-posted, with the original post deleted

Channel-management buttons are available without a separate ADMINS setting;
Telegram's own bot permissions still control access to each channel.

Multi-step flows (bind / unbind / set thumbnail / set caption) all funnel
through utils.state so a single follow-up message resumes exactly where
the user left off. Channel id can be supplied either by typing the
numeric id (e.g. -1001234567890) or by forwarding any post from the
target channel.
"""

import asyncio
import logging
import os
import time

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from config import COVER_DIR, DOWNLOAD_DIR
from database import db
from handlers.thumbnail import process_thumbnail
from utils.helpers import file_extension, get_media_duration, render_caption
from utils.keyboards import cancel_kb, channel_menu_kb, main_menu_kb
from handlers.cover import save_cover_from_message
from utils.pdf_pipeline import process_pdf
from utils.integrity import verify_download
from utils.fast_transfer import fast_download
from utils.sender import job_semaphore, send_verified, spawn, with_flood_retry
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return config.is_owner(user_id) or not config.ADMIN_IDS or user_id in config.ADMIN_IDS


def _extract_channel_id(message: Message) -> "int | None":
    """Accept either a typed numeric id or a forwarded channel post."""
    if message.forward_from_chat and message.forward_from_chat.id:
        return message.forward_from_chat.id
    text = (message.text or "").strip()
    try:
        return int(text)
    except ValueError:
        return None


# --------------------------------------------------------------------- #
# Text-flow processors — dispatched by handlers/common.py based on state
# --------------------------------------------------------------------- #
async def process_channel_bind_id(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    clear_state(user_id)
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text(
            "⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة (مثل -1001234567890) "
            "أو قم بإعادة توجيه أي منشور من القناة."
        )
        return

    try:
        chat = await client.get_chat(channel_id)
    except Exception as exc:
        await message.reply_text(f"❌ تعذّر الوصول إلى القناة: {exc}")
        return

    await db.bind_channel(channel_id, bound_by=user_id)
    await message.reply_text(
        f"✅ تم ربط البوت بقناة **{chat.title}** (`{channel_id}`).\n\n"
        "تأكد من أن البوت مشرف في القناة مع صلاحيات النشر والحذف.",
        reply_markup=channel_menu_kb(),
    )


async def process_channel_unbind_id(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    clear_state(user_id)
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return

    await db.unbind_channel(channel_id)
    await message.reply_text(
        f"🛑 تم إلغاء ربط القناة `{channel_id}`.", reply_markup=channel_menu_kb()
    )


async def process_channel_thumb_id(client: Client, message: Message) -> None:
    """Step 1 of 'set channel thumbnail': got the channel id, now ask for the photo."""
    user_id = message.from_user.id
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return

    set_state(user_id, UserState.AWAITING_CHANNEL_THUMB_PHOTO, channel_id=channel_id)
    await message.reply_text("📸 أرسل الآن الصورة المصغرة الخاصة بالقناة.", reply_markup=cancel_kb())


async def process_channel_thumb_photo(client: Client, message: Message) -> None:
    """Step 2 of 'set channel thumbnail': got the photo, save it."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    channel_id = entry.data.get("channel_id")
    clear_state(user_id)

    if channel_id is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الطلب. ابدأ من جديد من القائمة.")
        return

    status = await message.reply_text("⏳ جارٍ حفظ صورة القناة...")
    raw_path = os.path.join(DOWNLOAD_DIR, f"channel_{channel_id}_thumb.raw")
    final_path = os.path.join(DOWNLOAD_DIR, f"channel_{channel_id}_thumb.jpg")
    try:
        await client.download_media(message, file_name=raw_path)
        await asyncio.get_running_loop().run_in_executor(
            None, process_thumbnail, raw_path, final_path
        )
        await db.set_channel_thumbnail(channel_id, final_path)
        await status.edit_text("✅ تم حفظ صورة القناة المصغرة.", reply_markup=channel_menu_kb())
    except Exception as exc:
        logger.exception("Failed to set channel thumbnail for %s", channel_id)
        await status.edit_text(f"❌ فشل حفظ الصورة: {exc}")
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)


async def process_channel_caption_id(client: Client, message: Message) -> None:
    """Step 1 of 'set channel caption': got the channel id, now ask for the template."""
    user_id = message.from_user.id
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return

    set_state(user_id, UserState.AWAITING_CHANNEL_CAPTION_TEXT, channel_id=channel_id)
    await message.reply_text(
        "✏️ أرسل الآن نص التسمية التوضيحية للقناة.\n\n"
        "المتغيرات المتاحة: `{filename}`، `{filesize}`، `{duration}`",
        reply_markup=cancel_kb(),
    )


async def process_channel_caption_text(client: Client, message: Message) -> None:
    """Step 2 of 'set channel caption': got the template, save it."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    channel_id = entry.data.get("channel_id")
    clear_state(user_id)

    if channel_id is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الطلب. ابدأ من جديد من القائمة.")
        return

    await db.set_channel_caption(channel_id, message.text)
    await message.reply_text("✅ تم حفظ تسمية القناة التوضيحية.", reply_markup=channel_menu_kb())


async def process_channel_watermark_id(client: Client, message: Message) -> None:
    """Step 1 of 'set channel watermark patterns': got the channel id, now ask for the patterns."""
    user_id = message.from_user.id
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return

    set_state(user_id, UserState.AWAITING_CHANNEL_WATERMARK_PATTERNS, channel_id=channel_id)
    await message.reply_text(
        "🧽 أرسل الآن قواعد العلامة المائية للقناة، **سطر لكل قاعدة**.\n\n"
        "`نص عادي` — حذف حرفي\n"
        "`regex:النمط` — تعبير نمطي\n"
        "`link:جزء من الرابط` — حذف الروابط المضمّنة المطابقة\n"
        "أضف `=> البديل` للاستبدال بدل الحذف، مثال: `قناة_قديمة@ => قناة الكرفة`\n"
        "`link:t.me/old => https://t.me/new` — تغيير وجهة الرابط\n"
        "اختياري: سطر `font:amiri` (أو aref, rakkas, lalezar, tajawal, almarai, katibeh, "
        "mirza, scheherazade) وسطر `color:#C0392B` لتحديد شكل النص البديل.",
        reply_markup=cancel_kb(),
    )


async def process_channel_watermark_patterns(client: Client, message: Message) -> None:
    """Step 2 of 'set channel watermark patterns': got the patterns, save them."""
    user_id = message.from_user.id
    entry = get_state(user_id)
    channel_id = entry.data.get("channel_id")
    clear_state(user_id)

    if channel_id is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الطلب. ابدأ من جديد من القائمة.")
        return

    await db.set_channel_watermark_patterns(channel_id, message.text)
    await message.reply_text(
        "✅ تم حفظ أنماط إزالة العلامة المائية للقناة.", reply_markup=channel_menu_kb()
    )


async def process_channel_cover_id(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return
    set_state(user_id, UserState.AWAITING_CHANNEL_COVER_PHOTO, channel_id=channel_id)
    await message.reply_text(
        "📄 أرسل الآن الصورة التي ستوضع في أول صفحة من كل PDF يُنشر في القناة.\n\n"
        "💡 للجودة الكاملة أرسلها كـ «ملف» (File) وليس كصورة.",
        reply_markup=cancel_kb(),
    )


async def process_channel_cover_photo(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    channel_id = get_state(user_id).data.get("channel_id")
    clear_state(user_id)
    if channel_id is None:
        await message.reply_text("⚠️ انتهت صلاحية هذا الطلب. ابدأ من جديد من القائمة.")
        return
    status = await message.reply_text("⏳ جارٍ حفظ الصورة...")
    try:
        path = await save_cover_from_message(
            client, message, os.path.join(COVER_DIR, f"channel_{channel_id}")
        )
        await db.set_channel_cover(channel_id, path)
        await status.edit_text("✅ تم حفظ صورة أول صفحة للقناة.", reply_markup=channel_menu_kb())
    except Exception as exc:
        logger.exception("Failed to set channel cover for %s", channel_id)
        await status.edit_text(f"❌ فشل حفظ الصورة: {exc}")


async def process_channel_delcover_id(client: Client, message: Message) -> None:
    clear_state(message.from_user.id)
    channel_id = _extract_channel_id(message)
    if channel_id is None:
        await message.reply_text("⚠️ لم أفهم معرّف القناة. أرسل الرقم مباشرة.")
        return
    channel = await db.get_channel(channel_id)
    if channel and channel.channel_cover_path and os.path.exists(channel.channel_cover_path):
        os.remove(channel.channel_cover_path)
    await db.delete_channel_cover(channel_id)
    await message.reply_text("🗑 تم حذف صورة أول صفحة للقناة.", reply_markup=channel_menu_kb())


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:channel$"))
    async def menu_channel_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك باستخدام هذه الميزة.", show_alert=True)
            return
        await callback_query.message.edit_text(
            "📢 **إدارة القناة**\n\nاختر أحد الخيارات التالية:",
            reply_markup=channel_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:bind$"))
    async def channel_bind_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_BIND_ID)
        await callback_query.message.edit_text(
            "🔗 أرسل معرّف القناة الرقمي (مثل `-1001234567890`)، أو قم "
            "بإعادة توجيه أي منشور من القناة إليّ.\n\n"
            "تأكد من أن البوت مشرف في القناة أولًا.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:unbind$"))
    async def channel_unbind_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_UNBIND_ID)
        await callback_query.message.edit_text(
            "✂️ أرسل معرّف القناة المراد إلغاء ربطها، أو أعد توجيه منشور منها.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:set_thumb$"))
    async def channel_set_thumb_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_THUMB_ID)
        await callback_query.message.edit_text(
            "🖼 أرسل معرّف القناة أولًا (أو أعد توجيه منشور منها)، "
            "وسأطلب منك الصورة بعد ذلك.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:set_caption$"))
    async def channel_set_caption_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_CAPTION_ID)
        await callback_query.message.edit_text(
            "✏️ أرسل معرّف القناة أولًا (أو أعد توجيه منشور منها)، "
            "وسأطلب منك نص التسمية التوضيحية بعد ذلك.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:set_watermark$"))
    async def channel_set_watermark_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_WATERMARK_ID)
        await callback_query.message.edit_text(
            "🧽 أرسل معرّف القناة أولًا (أو أعد توجيه منشور منها)، "
            "وسأطلب منك الأنماط بعد ذلك.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:set_cover$"))
    async def channel_set_cover_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_COVER_ID)
        await callback_query.message.edit_text(
            "📄 أرسل معرّف القناة أولًا (أو أعد توجيه منشور منها)، وسأطلب منك الصورة بعد ذلك.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^channel:del_cover$"))
    async def channel_del_cover_cb(client: Client, callback_query: CallbackQuery) -> None:
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ غير مصرح لك.", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_CHANNEL_DELCOVER_ID)
        await callback_query.message.edit_text(
            "🗑 أرسل معرّف القناة (أو أعد توجيه منشور منها) لحذف صورة أول صفحة الخاصة بها.",
            reply_markup=cancel_kb(),
        )
        await callback_query.answer()

    # ------------------------------------------------------------------ #
    # The actual auto-formatter: intercept documents posted to a bound
    # channel and replace them with a reformatted version.
    # ------------------------------------------------------------------ #
    @app.on_message(filters.channel & filters.document)
    async def auto_format_channel_post(client: Client, message: Message) -> None:
        channel = await db.get_channel(message.chat.id)
        if not channel or not channel.is_active:
            return
        # Heavy work runs in the background; the handler returns at once.
        spawn(_format_channel_post(client, message, channel))


    async def _format_channel_post(client: Client, message: Message, channel) -> None:

        original_name = message.document.file_name or f"file_{message.id}"
        # Short local name (a long Arabic display name can exceed filesystem limits).
        download_path = os.path.join(
            DOWNLOAD_DIR,
            f"chan_{message.chat.id}_{message.id}_{int(time.time())}{file_extension(original_name)}",
        )
        file_path = None
        try:
            async with job_semaphore():
                file_path = await with_flood_retry(
                    lambda: fast_download(client, message, download_path)
                )
                verify_download(file_path, message.document.file_size or 0)

                if file_extension(file_path).lower() == ".pdf" or message.document.mime_type == "application/pdf":
                    cover = channel.channel_cover_path
                    await process_pdf(
                        file_path,
                        watermark_patterns=channel.channel_watermark_patterns,
                        cover_path=cover if cover and os.path.exists(cover) else None,
                    )

                caption = render_caption(
                    channel.channel_caption_template,
                    filename=original_name,
                    filesize=os.path.getsize(file_path),
                    duration=await asyncio.get_running_loop().run_in_executor(None, get_media_duration, file_path),
                )
                thumb_path = (
                    channel.default_thumbnail_path
                    if channel.default_thumbnail_path and os.path.exists(channel.default_thumbnail_path)
                    else None
                )
                # The original post is deleted below only if this succeeded AND
                # Telegram holds exactly the bytes we sent.
                await send_verified(
                    client, message.chat.id, "document", file_path,
                    original_name, caption, thumb=thumb_path,
                )
            await message.delete()
        except Exception:
            logger.exception("Channel auto-format failed for chat %s", message.chat.id)
        finally:
            for path in (file_path, download_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
