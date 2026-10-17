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
from utils import ui
from utils.keyboards import help_kb, main_menu_kb
from utils.state import clear_state

logger = logging.getLogger(__name__)

DEFAULT_WELCOME_TEXT = (
    "هلا بيك يا غالي انا الكارفة🔥 مرات الكارف🔥 عمك وعم عيالك طبعا "
    "جتلي بعد ما الكارف كرفلك كرفلي انا كمان 🙃\n"
    "المهم ابعتلي اي ملف انت عايز تعدله وسيب الباقي عليا 💕"
)


def welcome_text() -> str:
    """The /start message. The owner can override it from the admin panel."""
    return db.get_setting("welcome_text") or DEFAULT_WELCOME_TEXT

def menu_text() -> str:
    """The welcome message (kept exactly as written) + a tidy footer."""
    return f"{welcome_text()}\n\n{ui.THIN}\n✨ اختار من القائمة تحت 👇"


HELP_TEXT = (
    ui.header("💎", "دليل الاستخدام")
    + "\n"
    "📁 **إعادة تسمية ملف**\n"
    "ابعت أي مستند أو فيديو أو ملف صوتي، واختار «إعادة تسمية» أو «الاحتفاظ بالاسم».\n\n"
    "🖼 **الصورة المصغرة**\n"
    "اضبط صورة وهتتحط تلقائيًا على كل ملف يوصلك.\n\n"
    "✏️ **التسمية التوضيحية**\n"
    "نص مخصص تحت كل ملف، وتقدر تستخدم:\n"
    "`{filename}` اسم الملف  •  `{filesize}` الحجم  •  `{duration}` المدة\n\n"
    f"{ui.THIN}\n"
    "📄 **صورة أول صفحة في الـ PDF**\n"
    "احفظ صورة وهتتحط في أول صفحة (صفحة جديدة أو استبدال الأولى)، "
    "وتقدر تختار صورة لملف واحد بس أو تتخطاها.\n\n"
    "🧽 **العلامة المائية للـ PDF**\n"
    "حدد النصوص أو الروابط اللي تتشال أو تتبدّل، وتتطبق تلقائيًا قبل إعادة الرفع.\n\n"
    "✨ **تحسين جودة الـ PDF**\n"
    "أصغر حجم بأعلى جودة، والنصوص والرسومات بتفضل حادة.\n\n"
    "🧰 **استوديو PDF**\n"
    "دمج • تقسيم • استخراج • ترتيب • تدوير • ضغط • صور إلى PDF.\n\n"
    f"{ui.THIN}\n"
    "🎬 **الفيديو**\n"
    "علامة مائية (نص أو لوجو) وعنوان افتراضي، ولكل فيديو تقدر تفعّل أو تلغي أو تكتب عنوان خاص به.\n\n"
    "🔗 **الرابط المخصص**\n"
    "احفظ رابط البوت أو قناتك، واسم الملف يظهر كلينك قابل للضغط.\n\n"
    "📢 **وضع القناة (للمشرفين)**\n"
    "اربط البوت بقناة وهيعيد تنسيق كل ملف يتنشر فيها تلقائيًا."
)


def is_admin(user_id: int) -> bool:
    # ADMIN_IDS is optional; empty means everyone (previous behaviour).
    return config.is_owner(user_id) or not config.ADMIN_IDS or user_id in config.ADMIN_IDS


def register(app: Client) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client: Client, message: Message) -> None:
        await db.ensure_user(message.from_user.id)
        uid = message.from_user.id
        clear_state(uid)  # /start also leaves any open flow (e.g. the owner's chat mode)
        await message.reply_text(
            menu_text(),
            reply_markup=main_menu_kb(is_admin(uid), config.is_owner(uid)),
        )

    @app.on_callback_query(filters.regex(r"^menu:main$"))
    async def menu_main_cb(client: Client, callback_query: CallbackQuery) -> None:
        uid = callback_query.from_user.id
        await callback_query.message.edit_text(
            menu_text(),
            reply_markup=main_menu_kb(is_admin(uid), config.is_owner(uid)),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^menu:help$"))
    async def menu_help_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(HELP_TEXT, reply_markup=help_kb())
        await callback_query.answer()
