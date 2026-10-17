"""
handlers/watermark.py
=======================
Per-user watermark rules (remove / replace text and links) — fully
button-driven: rules, font, colour, and a before/after preview on a real PDF.

The rules saved here are applied by handlers/rename.py to every PDF a
user sends through the private renaming flow, right before it's
re-uploaded. See utils/pdf_watermark.py for the pattern syntax and how
matches are actually removed / replaced.
"""

import asyncio
import logging
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from config import DOWNLOAD_DIR
from database import db
from utils.helpers import file_extension
from utils.integrity import verify_download
from utils import ui
from utils.keyboards import (
    cancel_kb,
    watermark_colors_kb,
    watermark_fonts_kb,
    watermark_menu_kb,
    watermark_preview_done_kb,
)
from utils.pdf_watermark import (
    COLORS,
    FONTS,
    describe_pattern,
    merge_directives,
    parse_patterns,
    parse_style,
    set_directive,
)
from utils.sender import job_semaphore, with_flood_retry
from utils.state import UserState, clear_state, set_state
from utils.wm_preview import build_preview, render_font_gallery

PREVIEW_MAX_BYTES = 60 * 1024 * 1024  # a preview needs no giant files

logger = logging.getLogger(__name__)

_PATTERNS_HELP = (
    "🧽 أرسل الآن القواعد، **سطر لكل قاعدة**.\n\n"
    "**① حذف:**\n"
    "`نص عادي` — يحذف هذا النص من كل صفحة\n"
    "`regex:النمط` — تعبير نمطي\n"
    "`link:جزء من الرابط` — يحذف أي رابط مضمّن فيه هذا الجزء\n\n"
    "**② استبدال** — أضف `=>` وبعدها البديل:\n"
    "`نص قديم => نص جديد` — يكتب الجديد مكان القديم بالخط المختار\n"
    "`link:t.me/old => https://t.me/new` — يغيّر وجهة الرابط\n"
    "لو البديل شكله رابط (`t.me/x` أو `@name`) يبقى قابل للضغط تلقائيًا.\n\n"
    "**مثال:**\n"
    "```\n"
    "قناة_قديمة@ => قناة الكرفة\n"
    "regex:t\\.me/\\w+ => t.me/alkarfa\n"
    "link:example.com => https://t.me/alkarfa\n"
    "PIRATE.COM\n"
    "```\n"
    "💡 البديل الأقصر من الأصل بيطلع أنضف، والطويل بيصغّر خطه تلقائيًا عشان ما يركبش على اللي جنبه. "
    "الختم المائل بيتحذف بس مش بيتكتب مكانه بديل. "
    "جرّب النتيجة قبل الإنتاج من زر **🔍 معاينة على ملف PDF**."
)


def _sample_text(blob: "str | None") -> str:
    """The first replacement text the user wrote (for the font gallery), if any."""
    for p in parse_patterns(blob):
        if p.replacement and p.kind != "link":
            return p.replacement
    return "قناة الكرفة"


async def _edit(cq: CallbackQuery, text: str, kb) -> None:
    """Edit a menu message whether it is plain text or a photo with a caption."""
    try:
        if cq.message.photo:
            await cq.message.edit_caption(text, reply_markup=kb)
        else:
            await cq.message.edit_text(text, reply_markup=kb)
    except Exception:
        # MessageNotModified etc. — harmless
        pass


async def process_watermark_patterns(client: Client, message: Message) -> None:
    """Called by handlers/common.py when state == AWAITING_WATERMARK_PATTERNS."""
    user_id = message.from_user.id
    patterns = parse_patterns(message.text)
    if not patterns:
        await message.reply_text(
            "⚠️ مفيش أنماط في الرسالة. ابعت سطر لكل نمط، أو اضغط إلغاء.",
            reply_markup=cancel_kb(),
        )
        return
    # A broken regex would silently never match — tell the user right away.
    bad = []
    for p in patterns:
        if p.kind == "regex":
            try:
                re.compile(p.raw)
            except re.error:
                bad.append(p.raw)
    if bad:
        await message.reply_text(
            "⚠️ الـ regex ده فيه غلط ومش هيشتغل:\n" + "\n".join(f"`{b}`" for b in bad)
            + "\n\nصلّحه وابعت الأنماط تاني، أو اضغط إلغاء.",
            reply_markup=cancel_kb(),
        )
        return
    clear_state(user_id)
    user = await db.get_user(user_id)
    # keep the font / colour the user already picked
    blob = merge_directives(user.watermark_patterns if user else None, message.text)
    await db.set_user_watermark_patterns(user_id, blob)
    summary = "\n".join(describe_pattern(p) for p in patterns[:15])
    await message.reply_text(
        f"✅ تم حفظ {len(patterns)} قاعدة للعلامة المائية. هتتطبّق تلقائيًا "
        "على أي ملف PDF ترسله من الآن.\n"
        "جرّب شكل النتيجة بزر «🔍 معاينة على ملف PDF».\n\n" + summary,
        reply_markup=watermark_menu_kb(),
        parse_mode=ParseMode.DISABLED,
    )


async def process_preview_pdf(client: Client, message: Message) -> None:
    """The user sent a PDF while in AWAITING_WATERMARK_PREVIEW_PDF: show before / after."""
    user_id = message.from_user.id
    doc = message.document
    is_pdf = (doc.mime_type == "application/pdf") or (file_extension(doc.file_name or "").lower() == ".pdf")
    if not is_pdf:
        await message.reply_text("📄 المعاينة بتشتغل على ملفات PDF بس. ابعت PDF أو اضغط إلغاء.",
                                 reply_markup=cancel_kb())
        return
    if (doc.file_size or 0) > PREVIEW_MAX_BYTES:
        await message.reply_text("⚠️ الملف كبير على المعاينة (الحد 60 ميجا). ابعت ملف أصغر أو اضغط إلغاء.",
                                 reply_markup=cancel_kb())
        return

    user = await db.get_user(user_id)
    blob = user.watermark_patterns if user else None
    patterns = parse_patterns(blob)
    if not patterns:
        clear_state(user_id)
        await message.reply_text("⚠️ مفيش قواعد محفوظة تتجرّب. أضف قواعد الأول.",
                                 reply_markup=watermark_menu_kb())
        return
    style = parse_style(blob)

    status = await message.reply_text("🔍 جارٍ تجهيز المعاينة...")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    stamp = f"{user_id}_{int(time.time())}_{message.id}"
    pdf_path = os.path.join(DOWNLOAD_DIR, f"wmprev_{stamp}.pdf")
    png_path = os.path.join(DOWNLOAD_DIR, f"wmprev_{stamp}.png")
    try:
        async with job_semaphore():
            got = await with_flood_retry(lambda: client.download_media(message, file_name=pdf_path))
            verify_download(got, doc.file_size or 0)
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, build_preview, got, patterns, style, png_path)
        clear_state(user_id)

        font_label = FONTS[style.font][1].split(" — ")[0]
        if info.changes:
            caption = (
                f"✅ لقيت {info.changes} تعديل في {info.pages_hit} صفحة"
                + (f" (من أول {info.pages_scanned} صفحة)" if info.pages_scanned < info.total_pages else "")
                + f".\nالمعروض: صفحة {info.page_no} — الخط: {font_label}\n"
                "المربعات الحمرا هي اللي هتتعدّل. لو عجبتك ابعت الملف للتسمية والإنتاج كالمعتاد."
            )
        else:
            caption = (
                f"⚠️ مفيش أي تطابق في أول {info.pages_scanned} صفحة، فالملف هيتبعت زي ما هو.\n"
                "تأكد من كتابة النص زي ما هو في الملف (الحروف المتصلة والتشكيل مش فارقين)، "
                "أو جرّب regex: أو link:."
            )
        await message.reply_photo(png_path, caption=caption, reply_markup=watermark_preview_done_kb())
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Watermark preview failed for user %s", user_id)
        clear_state(user_id)
        await status.edit_text(f"❌ تعذّرت المعاينة: {exc}", reply_markup=watermark_menu_kb())
    finally:
        for path in (pdf_path, png_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:watermark$"))
    async def menu_watermark_cb(client: Client, callback_query: CallbackQuery) -> None:
        await callback_query.message.edit_text(
            ui.header("🧽", "العلامة المائية في الـ PDF")
            + "\nاحذف أي نص أو رابط، أو **استبدله** بنص عربي بخط جميل أو برابطك الجديد.\n"
            "القواعد هنا بتتطبّق تلقائيًا على كل PDF تبعته قبل إعادة رفعه.\n"
            f"\n{ui.THIN}\n✨ اختر من الأزرار بالأسفل 👇",
            reply_markup=watermark_menu_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:preview$"))
    async def watermark_preview_cb(client: Client, callback_query: CallbackQuery) -> None:
        user = await db.get_user(callback_query.from_user.id)
        if not parse_patterns(user.watermark_patterns if user else None):
            await callback_query.answer("أضف قواعد الأول من «تعيين/تحديث الأنماط».", show_alert=True)
            return
        set_state(callback_query.from_user.id, UserState.AWAITING_WATERMARK_PREVIEW_PDF)
        await _edit(
            callback_query,
            "🔍 ابعت ملف PDF (أي ملف عليه العلامة) وهعرضلك صفحة **قبل / بعد** "
            "من غير ما أعدّل ملفك ولا أرفعه.",
            cancel_kb(),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:fonts$"))
    async def watermark_fonts_cb(client: Client, callback_query: CallbackQuery) -> None:
        user_id = callback_query.from_user.id
        user = await db.get_user(user_id)
        blob = user.watermark_patterns if user else None
        style = parse_style(blob)
        await callback_query.answer("جارٍ تجهيز معرض الخطوط...")
        png = os.path.join(DOWNLOAD_DIR, f"wmfonts_{user_id}_{int(time.time())}.png")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, render_font_gallery, _sample_text(blob), png, style.color
            )
            await callback_query.message.reply_photo(
                png,
                caption="🔤 اضغط على الخط اللي عجبك. هيتطبق على النص البديل في كل ملفاتك.",
                reply_markup=watermark_fonts_kb(style.font),
            )
        finally:
            if os.path.exists(png):
                try:
                    os.remove(png)
                except OSError:
                    pass

    @app.on_callback_query(filters.regex(r"^watermark:font:(\w+)$"))
    async def watermark_font_cb(client: Client, callback_query: CallbackQuery) -> None:
        key = callback_query.matches[0].group(1)
        if key not in FONTS:
            await callback_query.answer("خط غير معروف.", show_alert=True)
            return
        user_id = callback_query.from_user.id
        user = await db.get_user(user_id)
        blob = set_directive(user.watermark_patterns if user else None, "font", key)
        await db.set_user_watermark_patterns(user_id, blob)
        await callback_query.answer(f"✅ الخط: {FONTS[key][1].split(' — ')[0]}")
        try:
            await callback_query.message.edit_reply_markup(watermark_fonts_kb(key))
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^watermark:colors$"))
    async def watermark_colors_cb(client: Client, callback_query: CallbackQuery) -> None:
        user = await db.get_user(callback_query.from_user.id)
        style = parse_style(user.watermark_patterns if user else None)
        await _edit(
            callback_query,
            "🎨 اختر لون النص البديل (أو خلّيه بنفس لون النص الأصلي):",
            watermark_colors_kb(style.color or ""),
        )
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^watermark:color:(\w+)$"))
    async def watermark_color_cb(client: Client, callback_query: CallbackQuery) -> None:
        key = callback_query.matches[0].group(1)
        if key != "auto" and key not in COLORS:
            await callback_query.answer("لون غير معروف.", show_alert=True)
            return
        user_id = callback_query.from_user.id
        user = await db.get_user(user_id)
        value = None if key == "auto" else COLORS[key][0]
        blob = set_directive(user.watermark_patterns if user else None, "color", value)
        await db.set_user_watermark_patterns(user_id, blob)
        await callback_query.answer("✅ تم تغيير اللون")
        try:
            await callback_query.message.edit_reply_markup(watermark_colors_kb(value or ""))
        except Exception:
            pass

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
            "🗑 تم حذف القواعد (والخط واللون المحفوظين). لن يتم تعديل ملفات الـ PDF بعد الآن.",
            reply_markup=watermark_menu_kb(),
        )
        await callback_query.answer()
