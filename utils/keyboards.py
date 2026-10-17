"""
utils/keyboards.py
====================
Every inline keyboard the bot shows lives here, in one place, so the
button layout and Arabic labels stay consistent across handlers.
"""

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config

BLUE, GREEN, RED = ButtonStyle.PRIMARY, ButtonStyle.SUCCESS, ButtonStyle.DANGER


def developer_button() -> InlineKeyboardButton:
    """
    Opens the developer's Telegram profile directly (a URL button, no bot round-trip).
    Always sits alone on its own full-width row: «𝑫𝒆𝒗: @username».
    """
    return InlineKeyboardButton(
        f"𝑫𝒆𝒗: @{config.DEVELOPER_USERNAME}", url=config.DEVELOPER_URL, style=BLUE
    )


def main_menu_kb(is_admin: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🖼 الصورة المصغرة", callback_data="menu:thumb", style=BLUE),
            InlineKeyboardButton("✏️ التسمية التوضيحية", callback_data="menu:caption", style=BLUE),
        ],
        [
            InlineKeyboardButton("📄 غلاف الـ PDF", callback_data="menu:cover", style=GREEN),
            InlineKeyboardButton("🧽 العلامة المائية", callback_data="menu:watermark", style=GREEN),
        ],
        [
            InlineKeyboardButton("🎬 إعدادات الفيديو", callback_data="menu:video", style=BLUE),
            InlineKeyboardButton("🔗 الرابط المخصص", callback_data="menu:link", style=BLUE),
        ],
        [InlineKeyboardButton("🧰 استوديو PDF ✨", callback_data="studio:home", style=GREEN)],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("📢 إدارة القناة", callback_data="menu:channel", style=BLUE)])
    if is_owner:
        rows.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="admin:home", style=RED)])
    rows.append([InlineKeyboardButton("💎 دليل الاستخدام", callback_data="menu:help", style=BLUE)])
    rows.append([developer_button()])
    return InlineKeyboardMarkup(rows)


def help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main", style=BLUE)],
            [developer_button()],
        ]
    )


def thumb_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين صورة جديدة", callback_data="thumb:set")],
            [InlineKeyboardButton("👁 عرض الصورة الحالية", callback_data="thumb:show")],
            [InlineKeyboardButton("🗑 حذف الصورة", callback_data="thumb:del")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def caption_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين تسمية جديدة", callback_data="caption:set")],
            [InlineKeyboardButton("👁 عرض التسمية الحالية", callback_data="caption:show")],
            [InlineKeyboardButton("🗑 حذف التسمية", callback_data="caption:del")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def watermark_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ تعيين/تحديث الأنماط", callback_data="watermark:set")],
            [InlineKeyboardButton("🔍 معاينة على ملف PDF", callback_data="watermark:preview")],
            [
                InlineKeyboardButton("🔤 الخط", callback_data="watermark:fonts"),
                InlineKeyboardButton("🎨 اللون", callback_data="watermark:colors"),
            ],
            [InlineKeyboardButton("👁 عرض الأنماط الحالية", callback_data="watermark:show")],
            [InlineKeyboardButton("🗑 حذف الأنماط", callback_data="watermark:del")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def watermark_fonts_kb(current: str = "") -> InlineKeyboardMarkup:
    from utils.pdf_watermark import FONTS

    buttons = [
        InlineKeyboardButton(
            ("✅ " if key == current else "") + f"{i}. {label.split(' — ')[0]}",
            callback_data=f"watermark:font:{key}",
        )
        for i, (key, (_f, label)) in enumerate(FONTS.items(), start=1)
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("🎨 اللون", callback_data="watermark:colors")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:watermark")])
    return InlineKeyboardMarkup(rows)


def watermark_colors_kb(current: str = "") -> InlineKeyboardMarkup:
    from utils.pdf_watermark import COLORS

    buttons = [
        InlineKeyboardButton(
            ("✅ " if hexa == current else "") + label, callback_data=f"watermark:color:{key}"
        )
        for key, (hexa, label) in COLORS.items()
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("🪄 نفس لون النص الأصلي", callback_data="watermark:color:auto")])
    rows.append([InlineKeyboardButton("🔤 الخط", callback_data="watermark:fonts")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:watermark")])
    return InlineKeyboardMarkup(rows)


def watermark_preview_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 معاينة ملف تاني", callback_data="watermark:preview")],
            [
                InlineKeyboardButton("🔤 الخط", callback_data="watermark:fonts"),
                InlineKeyboardButton("🎨 اللون", callback_data="watermark:colors"),
            ],
            [InlineKeyboardButton("🧽 قائمة العلامة المائية", callback_data="menu:watermark")],
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
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
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
    optimize_active: bool = False,
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
        opt_label = (
            "✨ تحسين جودة الـ PDF وأقل حجم: ✅" if optimize_active
            else "✨ تحسين جودة الـ PDF وأقل حجم: 🚫"
        )
        rows.append([InlineKeyboardButton(opt_label, callback_data=f"rename:opt:{msg_id}")])
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
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def thumb_saved_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 استخدمها كصورة أول صفحة PDF كمان", callback_data="thumb:to_cover")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def cover_saved_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖼 استخدمها كصورة مصغرة كمان", callback_data="cover:to_thumb")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


# --------------------------------------------------------------------- #
# Custom link (the file name in the caption becomes a link)
# --------------------------------------------------------------------- #
def link_menu_kb(has_link: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🤖 استخدم رابط البوت", callback_data="link:bot")],
        [InlineKeyboardButton("📢 رابط قناتي / رابط آخر", callback_data="link:set")],
    ]
    if has_link:
        rows.append([InlineKeyboardButton("👁 عرض الرابط الحالي", callback_data="link:show")])
        rows.append([InlineKeyboardButton("🗑 حذف الرابط", callback_data="link:del")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# PDF studio
# --------------------------------------------------------------------- #
def studio_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 دمج ملفات", callback_data="studio:tool:merge"),
                InlineKeyboardButton("✂️ تقسيم ملف", callback_data="studio:tool:split"),
            ],
            [
                InlineKeyboardButton("📑 استخراج صفحات", callback_data="studio:tool:extract"),
                InlineKeyboardButton("🔀 ترتيب الصفحات", callback_data="studio:tool:reorder"),
            ],
            [
                InlineKeyboardButton("🔄 تدوير الصفحات", callback_data="studio:tool:rotate"),
                InlineKeyboardButton("🗜 ضغط وتحسين", callback_data="studio:tool:compress"),
            ],
            [InlineKeyboardButton("🖼 تحويل صور إلى PDF", callback_data="studio:tool:images")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


def studio_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧰 استوديو PDF", callback_data="studio:home")]]
    )


def studio_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")]]
    )


def studio_collect_kb(count: int, tool: str, layout_label: str = "") -> InlineKeyboardMarkup:
    go = "✅ دمج الآن" if tool == "merge" else "✅ تحويل إلى PDF الآن"
    rows = [[InlineKeyboardButton(f"{go} ({count})", callback_data="studio:go")]]
    if tool == "images":
        rows.append([InlineKeyboardButton(f"📐 المقاس: {layout_label}", callback_data="studio:layout")])
    rows.append([InlineKeyboardButton("↩️ تراجع عن آخر ملف", callback_data="studio:undo")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")])
    return InlineKeyboardMarkup(rows)


def studio_levels_kb() -> InlineKeyboardMarkup:
    from utils.pdf_optimize import LEVELS

    rows = [
        [InlineKeyboardButton(level.button, callback_data=f"studio:lvl:{key}")]
        for key, level in LEVELS.items()
    ]
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")])
    return InlineKeyboardMarkup(rows)


def studio_split_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✂️ كل N صفحات في ملف", callback_data="studio:split:every")],
            [InlineKeyboardButton("🔢 عدد أجزاء متساوية", callback_data="studio:split:parts")],
            [InlineKeyboardButton("📑 حسب نطاقات (1-5,6-10)", callback_data="studio:split:ranges")],
            [InlineKeyboardButton("📄 كل صفحة في ملف", callback_data="studio:split:each")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")],
        ]
    )


def studio_rotate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("↻ 90° يمين", callback_data="studio:rot:90"),
                InlineKeyboardButton("↺ 90° يسار", callback_data="studio:rot:270"),
            ],
            [InlineKeyboardButton("🔃 180°", callback_data="studio:rot:180")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")],
        ]
    )


def studio_rotate_pages_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 كل الصفحات", callback_data="studio:rotall")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")],
        ]
    )


def studio_reorder_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔃 عكس ترتيب كل الصفحات", callback_data="studio:reverse")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="studio:cancel")],
        ]
    )
