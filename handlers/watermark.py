"""
handlers/watermark.py
=======================
Per-user watermark-removal pattern management — fully button-driven.

The patterns saved here are applied by handlers/rename.py to every PDF a
user sends through the private renaming flow, right before it's
re-uploaded. See utils/pdf_watermark.py for the pattern syntax and how
matches are actually removed.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from database import db
from utils.keyboards import cancel_kb, watermark_menu_kb
from utils.state import UserState, clear_state, set_state

logger = logging.getLogger(__name__)

_PATTERNS_HELP = (
    "🧽 أرسل الآن الأنماط المراد إزالتها من ملفات الـ PDF، **سطر لكل نمط**.\n\n"
    "**الأنواع المدعومة:**\n"
    "`نص عادي` — يبحث عن هذا النص حرفيًا في الصفحة ويزيله\n"
    "`regex:النمط` — تعبير نمطي (regex) يُطابق أي نص يحققه\n"
    "`link:جزء من الرابط` — يحذف أي رابط مضمّن يحتوي على هذا الجزء\n\n"
    "**مثال:**\n"
    "قناة_قديمة@\n"
    "regex:t\\.me/\\w+\n"
    "link:example.com"
)


async def process_watermark_patterns(client: Client, message: Message) -> None:
    """Called by handlers/common.py when state == AWAITING_WATERMARK_PATTERNS."""
    user_id = message.from_user.id
    clear_state(user_id)
    await db.set_user_watermark_patterns(user_id, message.text)
    await message.reply_text(
        "✅ تم حفظ أنماط إزالة العلامة المائية. سيتم تطبيقها تلقائيًا على "
        "أي ملف PDF ترسله من الآن.",
        reply_markup=watermark_menu_kb(),
    )


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:watermark$"))
    async def menu_watermark_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(
            "🧽 **إزالة العلامة المائية من ملفات PDF**\n\n"
            "تُطبَّق الأنماط المحفوظة هنا تلقائيًا على كل ملف PDF ترسله قبل "
            "إعادة رفعه. اختر أحد الخيارات التالية:",
            reply_markup=watermark_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:set$"))
    async def watermark_set_cb(client: Client, callback_query: CallbackQuery) -> None:
        set_state(callback_query.from_user.id, UserState.AWAITING_WATERMARK_PATTERNS)
        await callback_query.message.edit_text(_PATTERNS_HELP, reply_markup=cancel_kb())
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:show$"))
    async def watermark_show_cb(client: Client, callback_query: CallbackQuery) -> None:
        user = await db.get_user(callback_query.from_user.id)
        if not user or not user.watermark_patterns:
            await callback_query.answer("لا توجد أنماط محفوظة بعد.", show_alert=True)
            return
        await callback_query.message.reply_text(
            f"**أنماطك الحالية:**\n\n`{user.watermark_patterns}`",
            reply_markup=watermark_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:del$"))
    async def watermark_del_cb(client: Client, callback_query: CallbackQuery) -> None:
        user_id = callback_query.from_user.id
        await db.delete_user_watermark_patterns(user_id)
        await callback_query.message.edit_text(
            "🗑 تم حذف الأنماط. لن يتم تعديل ملفات الـ PDF بعد الآن.",
            reply_markup=watermark_menu_kb(),
        )
        await callback_query.answer()
