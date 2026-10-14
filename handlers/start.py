"""
handlers/start.py
===================
/start is the only slash command left in the bot — Telegram requires some
text entry point to open a chat with a bot. Everything else is driven by
inline-keyboard buttons rooted at the main menu built here.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from database import db
from utils.keyboards import help_kb, main_menu_kb

logger = logging.getLogger(__name__)

DEFAULT_WELCOME_TEXT = (
    "هلا بيك يا غالي انا الكارفة🔥 مرات الكارف🔥 عمك وعم عيالك طبعا "
    "جتلي بعد ما الكارف كرفلك كرفلي انا كمان 🙃\n"
    "المهم ابعتلي اي ملف انت عايز تعدله وسيب الباقي عليا 💕"
)


def welcome_text() -> str:
    """The /start message. The owner can override it from the admin panel."""
    return db.get_setting("welcome_text") or DEFAULT_WELCOME_TEXT

HELP_TEXT = (
    "**ℹ️ كيفية استخدام البوت**\n\n"
    "📁 **إعادة تسمية ملف**\n"
    "أرسل أي مستند أو فيديو أو ملف صوتي، ثم اختر «إعادة تسمية» واكتب "
    "الاسم الجديد، أو اختر «الاحتفاظ بالاسم الحالي».\n\n"
    "🖼 **الصورة المصغرة**\n"
    "من قائمة «الصورة المصغرة» يمكنك تعيين صورة أو عرضها أو حذفها. سيتم "
    "تصغيرها تلقائيًا وإرفاقها بكل ملف يتم إرساله لك.\n\n"
    "✏️ **التسمية التوضيحية**\n"
    "اضبط نص تسمية توضيحية مخصص من قائمة «التسمية التوضيحية». "
    "المتغيرات المتاحة:\n"
    "`{filename}` — اسم الملف\n"
    "`{filesize}` — حجم الملف\n"
    "`{duration}` — مدة الفيديو أو الصوت\n\n"
    "📄 **صورة أول صفحة في الـ PDF**\n"
    "من «صورة أول صفحة في الـ PDF» احفظ صورة ليضعها البوت تلقائيًا في أول صفحة "
    "من أي PDF ترسله (صفحة جديدة أو استبدال الأولى). ولو أرسلت PDF ستجد زر "
    "اختيار صورة لهذا الملف فقط، وتغيير الوضع، أو تخطي الصورة.\n\n"
    "🧽 **إزالة العلامة المائية من PDF**\n"
    "من قائمة «إزالة العلامة المائية» اضبط الأنماط (نص/regex/رابط) المراد "
    "إزالتها، وسيتم تطبيقها تلقائيًا على كل ملف PDF ترسله قبل إعادة رفعه.\n\n"
    "🎬 **الفيديو**\n"
    "من «إعدادات الفيديو» اضبط علامة مائية (نص أو لوجو، ومكانها وحجمها وشفافيتها) "
    "وعنوانًا افتراضيًا. وعند إرسال فيديو ستجد أزرارًا لتفعيل/إلغاء العلامة وكتابة "
    "عنوان لهذا الفيديو فقط.\n\n"
    "📢 **وضع القناة (للمشرفين)**\n"
    "من قائمة «إدارة القناة» يمكن للمشرف ربط البوت بقناة ليقوم تلقائيًا "
    "بإعادة تنسيق كل ملف يُنشر فيها (صورة مصغرة + تسمية توضيحية موحّدة، "
    "مع إزالة العلامة المائية ووضع صورة أول صفحة لملفات PDF إذا ضُبطت للقناة)."
)


def is_admin(user_id: int) -> bool:
    # ADMIN_IDS is optional; empty means everyone (previous behaviour).
    return config.is_owner(user_id) or not config.ADMIN_IDS or user_id in config.ADMIN_IDS


def register(app: Client) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client: Client, message: Message) -> None:
        await db.ensure_user(message.from_user.id)
        uid = message.from_user.id
        await message.reply_text(
            welcome_text(),
            reply_markup=main_menu_kb(is_admin(uid), config.is_owner(uid)),
        )

    @app.on_callback_query(filters.regex(r"^menu:main$"))
    async def menu_main_cb(client: Client, callback_query: CallbackQuery) -> None:
        uid = callback_query.from_user.id
        await callback_query.message.edit_text(
            welcome_text(),
            reply_markup=main_menu_kb(is_admin(uid), config.is_owner(uid)),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^menu:help$"))
    async def menu_help_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(HELP_TEXT, reply_markup=help_kb())
        await callback_query.answer()
