"""
handlers/caption.py
=====================
Per-user custom caption template management — fully button-driven.

Templates support {filename}, {filesize}, {duration} placeholders, filled
in at send-time by utils.helpers.render_caption.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from database import db
from utils import ui
from utils.keyboards import cancel_kb, caption_menu_kb
from utils.state import UserState, clear_state, set_state

logger = logging.getLogger(__name__)

_PLACEHOLDER_HELP = (
    "✏️ أرسل الآن نص التسمية التوضيحية الجديد.\n\n"
    "**المتغيرات المتاحة:**\n"
    "`{filename}` — اسم الملف\n"
    "`{filesize}` — حجم الملف\n"
    "`{duration}` — المدة (للفيديو/الصوت)\n\n"
    "💡 ضع `{filename}` في **آخر سطر** ليظهر عنوان الملف تحت في الرسالة "
    "(وجواه اللينك المخصص لو ضابطه).\n\n"
    "**مثال:**\n"
    "📦 {filesize} | ⏱ {duration}\n\n{filename}"
)


async def process_caption_text(client: Client, message: Message) -> None:
    """Called by handlers/common.py when state == AWAITING_CAPTION_TEXT."""
    user_id = message.from_user.id
    clear_state(user_id)
    await db.set_user_caption(user_id, message.text)
    await message.reply_text("✅ تم حفظ التسمية التوضيحية.", reply_markup=caption_menu_kb())


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:caption$"))
    async def menu_caption_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(
            ui.screen("✏️", "التسمية التوضيحية", "نص مخصص يظهر تحت كل ملف يوصلك.", "اختر من الأزرار بالأسفل 👇"),
            reply_markup=caption_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^caption:set$"))
    async def caption_set_cb(client: Client, callback_query: CallbackQuery) -> None:
        set_state(callback_query.from_user.id, UserState.AWAITING_CAPTION_TEXT)
        await callback_query.message.edit_text(_PLACEHOLDER_HELP, reply_markup=cancel_kb())
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^caption:show$"))
    async def caption_show_cb(client: Client, callback_query: CallbackQuery) -> None:
        user = await db.get_user(callback_query.from_user.id)
        if not user or not user.custom_caption:
            await callback_query.answer("لا توجد تسمية توضيحية محفوظة بعد.", show_alert=True)
            return
        await callback_query.message.reply_text(
            f"**تسميتك التوضيحية الحالية:**\n\n`{user.custom_caption}`",
            reply_markup=caption_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^caption:del$"))
    async def caption_del_cb(client: Client, callback_query: CallbackQuery) -> None:
        user_id = callback_query.from_user.id
        await db.delete_user_caption(user_id)
        await callback_query.message.edit_text(
            "🗑 تم حذف التسمية التوضيحية. سيتم استخدام التسمية الافتراضية الآن.",
            reply_markup=caption_menu_kb(),
        )
        await callback_query.answer()
