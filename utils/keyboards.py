"""
utils/keyboards.py
====================
Every inline keyboard the bot shows lives here, in one place, so the
button layout and Arabic labels stay consistent across handlers.
"""

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb(is_admin: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🖼 الصورة المصغرة", callback_data="menu:thumb")],
        [InlineKeyboardButton("📄 صورة أول صفحة في الـ PDF", callback_data="menu:cover")],
        [InlineKeyboardButton("✏️ التسمية التوضيحية", callback_data="menu:caption")],
        [InlineKeyboardButton("🧽 إزالة العلامة المائية", callback_data="menu:watermark")],
        [InlineKeyboardButton("🎬 إعدادات الفيديو", callback_data="menu:video")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("📢 إدارة القناة", callback_data="menu:channel")])
    if is_owner:
        rows.append([InlineKeyboardButton("👑 لوحة الأدمن", callback_data="admin:home")])
    rows.append([InlineKeyboardButton("ℹ️ المساعدة", callback_data="menu:help")])
    return InlineKeyboardMarkup(rows)


def help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")]]
    )


def thumb_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين صورة جديدة", callback_data="thumb:set")],
            [InlineKeyboardButton("👁 عرض الصورة الحالية", callback_data="thumb:show")],
            [InlineKeyboardButton("🗑 حذف الصورة", callback_data="thumb:del")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def caption_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين تسمية جديدة", callback_data="caption:set")],
            [InlineKeyboardButton("👁 عرض التسمية الحالية", callback_data="caption:show")],
            [InlineKeyboardButton("🗑 حذف التسمية", callback_data="caption:del")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def watermark_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين/تحديث الأنماط", callback_data="watermark:set")],
            [InlineKeyboardButton("👁 عرض الأنماط الحالية", callback_data="watermark:show")],
            [InlineKeyboardButton("🗑 حذف الأنماط", callback_data="watermark:del")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def channel_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 ربط قناة", callback_data="channel:bind")],
            [InlineKeyboardButton("✂️ إلغاء ربط قناة", callback_data="channel:unbind")],
            [InlineKeyboardButton("🖼 تعيين صورة القناة", callback_data="channel:set_thumb")],
            [InlineKeyboardButton("✏️ تعيين تسمية القناة", callback_data="channel:set_caption")],
            [
                InlineKeyboardButton(
                    "🧽 تعيين أنماط إزالة العلامة المائية", callback_data="channel:set_watermark"
                )
            ],
            [InlineKeyboardButton("📄 تعيين صورة أول صفحة للقناة", callback_data="channel:set_cover")],
            [InlineKeyboardButton("🗑 حذف صورة أول صفحة للقناة", callback_data="channel:del_cover")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])


def back_kb(target: str = "menu:main", label: str = "🔙 رجوع") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


def rename_prompt_kb(
    msg_id: int,
    is_pdf: bool = False,
    cover_active: bool = False,
    has_custom_cover: bool = False,
    has_saved_cover: bool = False,
    mode_label: str = "",
    is_video: bool = False,
    wm_active: bool = False,
    title_active: bool = False,
) -> InlineKeyboardMarkup:
    """
    Keyboard shown under every received file. `msg_id` is baked into every
    callback so several files can be pending at once.
    """
    rows = [
        [InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename:ask:{msg_id}")],
        [InlineKeyboardButton("📁 الاحتفاظ بالاسم الحالي", callback_data=f"rename:keep:{msg_id}")],
    ]
    if is_pdf:
        pick_label = "🔄 تغيير صورة أول صفحة" if has_custom_cover else "🖼 اختيار صورة لأول صفحة"
        rows.append([InlineKeyboardButton(pick_label, callback_data=f"rename:cover:{msg_id}")])
        if cover_active:
            rows.append([InlineKeyboardButton(f"📌 الوضع: {mode_label}", callback_data=f"rename:mode:{msg_id}")])
            rows.append([InlineKeyboardButton("🚫 بدون صورة لهذا الملف", callback_data=f"rename:nocover:{msg_id}")])
        elif has_saved_cover:
            rows.append([InlineKeyboardButton("♻️ استخدام صورتي المحفوظة", callback_data=f"rename:usesaved:{msg_id}")])
    if is_video:
        wm_label = "💧 علامة مائية لهذا الفيديو: ✅" if wm_active else "💧 علامة مائية لهذا الفيديو: 🚫"
        rows.append([InlineKeyboardButton(wm_label, callback_data=f"rename:vwm:{msg_id}")])
        title_label = "🔄 تغيير عنوان الفيديو" if title_active else "🏷 عنوان لهذا الفيديو"
        rows.append([InlineKeyboardButton(title_label, callback_data=f"rename:vtitle:{msg_id}")])
        if title_active:
            rows.append([InlineKeyboardButton("🚫 بدون عنوان", callback_data=f"rename:vtitleclear:{msg_id}")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data=f"rename:cancel:{msg_id}")])
    return InlineKeyboardMarkup(rows)


def confirm_thumb_kb(as_file_option: bool = False) -> InlineKeyboardMarkup:
    extra = (
        [[InlineKeyboardButton("📁 التعامل معها كملف عادي (إعادة تسمية)", callback_data="thumb:as_file")]]
        if as_file_option
        else []
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ استخدمها للاثنين (مصغرة + أول صفحة)", callback_data="thumb:confirm_both")],
            [InlineKeyboardButton("🖼 استخدمها كصورة مصغرة فقط", callback_data="thumb:confirm_yes")],
            [InlineKeyboardButton("📄 استخدمها لأول صفحة PDF فقط", callback_data="thumb:confirm_cover")],
        ]
        + extra
        + [[InlineKeyboardButton("❌ لا شيء", callback_data="thumb:confirm_no")]]
    )


def cover_menu_kb(enabled: bool, mode_label: str) -> InlineKeyboardMarkup:
    toggle = "🟢 التطبيق التلقائي: مفعّل" if enabled else "⚪️ التطبيق التلقائي: متوقف"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين / تغيير الصورة", callback_data="cover:set")],
            [InlineKeyboardButton("👁 عرض الصورة الحالية", callback_data="cover:show")],
            [InlineKeyboardButton(toggle, callback_data="cover:toggle")],
            [InlineKeyboardButton(f"📌 الوضع: {mode_label}", callback_data="cover:mode")],
            [InlineKeyboardButton("🗑 حذف الصورة", callback_data="cover:del")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def thumb_saved_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 استخدمها كصورة أول صفحة PDF كمان", callback_data="thumb:to_cover")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def cover_saved_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖼 استخدمها كصورة مصغرة كمان", callback_data="cover:to_thumb")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )
