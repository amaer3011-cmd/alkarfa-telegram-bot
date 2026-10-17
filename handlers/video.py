"""
handlers/video.py
===================
"🎬 إعدادات الفيديو": every user can configure, for the videos they send:

* a watermark — text or a logo image — with position / size / opacity,
* a default title template (supports {filename}),
* whether the title is also shown on screen for the first seconds.

The settings are applied automatically to each video; handlers/rename.py
also lets the user override the watermark and the title per file.
"""

import asyncio
import logging
import os
import time
from typing import Optional, Tuple

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    Message,
)
from PIL import Image

import config
from database import db
from utils import video_edit as ve
from utils import ui
from utils.keyboards import cancel_kb
from utils.state import UserState, clear_state, set_state

logger = logging.getLogger(__name__)

# Names, watermark text and title templates are user text: show them as
# plain text so stray * _ ` characters can't break the formatting.
_PM = ParseMode.DISABLED

MAX_WM_TEXT = 60
MAX_TITLE_TEMPLATE = 200
LOGO_MAX_WIDTH = 1024

EMOJI_NOTE = "ملحوظة: الإيموجي (زي 🔥) مش بتظهر على الفيديو، هتتشال من النص."
LOGO_TIP = (
    "💡 ابعتي اللوجو كـ **ملف (File)** بصيغة PNG عشان الخلفية الشفافة تفضل شفافة. "
    "لو بعتّيه كصورة عادية هيظهر بخلفيته."
)


def wm_configured(vs: db.VideoSettings) -> bool:
    return ve.WatermarkSpec(
        vs.wm_mode, vs.wm_text, vs.wm_image_path, vs.wm_position, vs.wm_size, vs.wm_opacity
    ).usable()


def spec_from_settings(vs: db.VideoSettings) -> ve.WatermarkSpec:
    return ve.WatermarkSpec(
        mode=vs.wm_mode, text=vs.wm_text, image_path=vs.wm_image_path,
        position=vs.wm_position, size_pct=vs.wm_size, opacity=vs.wm_opacity,
    )


def render_title(template: Optional[str], filename: str) -> Optional[str]:
    """Fill the default title template. Only {filename} is supported."""
    if not template:
        return None
    stem = os.path.splitext(filename)[0]
    return template.replace("{filename}", stem).strip() or None


# --------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------- #
def _wm_summary(vs: db.VideoSettings) -> str:
    if not wm_configured(vs):
        return "لسه ما اتضبطتش"
    what = f"نص: {vs.wm_text}".replace("\n", " / ") if vs.wm_mode == "text" else "صورة (لوجو)"
    return what


async def _menu(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    vs = await db.get_video_settings(user_id)
    state = "✅ مفعّلة" if (vs.wm_enabled and wm_configured(vs)) else "⚪️ متوقفة"
    text = (
        ui.plain_header("🎬", "إعدادات الفيديو")
        + f"\n💧 العلامة المائية ‹ {state}\n"
        f"   • {_wm_summary(vs)}\n"
        f"   • المكان: {ve.POSITION_LABELS.get(vs.wm_position, vs.wm_position)}"
        f" | الحجم: {ve.SIZE_LABELS.get(vs.wm_size, vs.wm_size)}"
        f" | الشفافية: {vs.wm_opacity}%\n\n"
        f"{ui.THIN}\n"
        f"🏷 قالب العنوان ‹ {vs.title_template or 'مفيش'}\n"
        f"📺 العنوان على الشاشة (أول {config.TITLE_OVERLAY_SECONDS} ثواني) ‹ "
        f"{'✅' if vs.title_overlay else '⚪️'}\n\n"
        "💡 أي فيديو تبعتيه هتلاقي تحته أزرار تتحكمي بيها في العلامة والعنوان "
        "للفيديو ده بس."
    )
    toggle = "💧 العلامة المائية: مفعّلة ✅" if (vs.wm_enabled and wm_configured(vs)) else "💧 العلامة المائية: متوقفة ⚪️"
    kb = InlineKeyboardMarkup(
        [
            [Btn(toggle, callback_data="video:wm_toggle")],
            [Btn("✏️ نص العلامة", callback_data="video:wm_text"),
             Btn("🖼 لوجو للعلامة", callback_data="video:wm_image")],
            [Btn(f"📍 {ve.POSITION_LABELS.get(vs.wm_position, '?')}", callback_data="video:wm_pos"),
             Btn(f"📏 {ve.SIZE_LABELS.get(vs.wm_size, '?')}", callback_data="video:wm_size"),
             Btn(f"🌫 {vs.wm_opacity}%", callback_data="video:wm_opacity")],
            [Btn("👁 معاينة العلامة", callback_data="video:wm_show"),
             Btn("🗑 حذف العلامة", callback_data="video:wm_del")],
            [Btn("🏷 قالب العنوان الافتراضي", callback_data="video:title_tpl")],
            [Btn(f"📺 العنوان على الشاشة: {'✅' if vs.title_overlay else '⚪️'}",
                 callback_data="video:title_overlay")],
            [Btn("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )
    return text, kb


async def _show_menu_edit(cq: CallbackQuery) -> None:
    text, kb = await _menu(cq.from_user.id)
    try:
        await cq.message.edit_text(text, reply_markup=kb, parse_mode=_PM)
    except MessageNotModified:
        pass


async def _show_menu_reply(message: Message) -> None:
    text, kb = await _menu(message.from_user.id)
    await message.reply_text(text, reply_markup=kb, parse_mode=_PM)


# --------------------------------------------------------------------- #
# Text / image flows (dispatched from handlers/common.py by state)
# --------------------------------------------------------------------- #
async def process_wm_text(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    clear_state(user_id)
    raw = ve.strip_emoji(message.text or "").strip()
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()][:3]
    text = "\n".join(lines)
    if not text:
        await message.reply_text("⚠️ النص فاضي (الإيموجي لوحدها مش بتظهر). جرّبي نص تاني.")
        return
    if len(text) > MAX_WM_TEXT:
        await message.reply_text(f"⚠️ النص طويل. الحد الأقصى {MAX_WM_TEXT} حرف.")
        return
    await db.update_video_settings(user_id, wm_text=text, wm_mode="text", wm_enabled=True)
    await _show_menu_reply(message)


def _save_logo(src: str, dest: str) -> None:
    img = Image.open(src)
    img.load()
    img = img.convert("RGBA")
    if img.width > LOGO_MAX_WIDTH:
        ratio = LOGO_MAX_WIDTH / img.width
        img = img.resize((LOGO_MAX_WIDTH, max(1, int(img.height * ratio))), Image.LANCZOS)
    img.save(dest, "PNG")


async def process_wm_image(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    clear_state(user_id)
    status = await message.reply_text("⏳ جارٍ تجهيز اللوجو...")
    tmp = os.path.join(config.VIDEO_WM_DIR, f"tmp_{user_id}_{int(time.time())}")
    dest = os.path.join(config.VIDEO_WM_DIR, f"{user_id}.png")
    downloaded: Optional[str] = None
    try:
        downloaded = await client.download_media(message, file_name=tmp)
        if not downloaded:
            raise RuntimeError("تعذّر تنزيل الصورة")
        await asyncio.get_running_loop().run_in_executor(None, _save_logo, downloaded, dest)
    except Exception as exc:
        logger.exception("Logo save failed for user %s", user_id)
        await status.edit_text(f"❌ فشل تجهيز اللوجو: {exc}")
        return
    finally:
        if downloaded and os.path.exists(downloaded):
            os.remove(downloaded)
    await db.update_video_settings(user_id, wm_image_path=dest, wm_mode="image", wm_enabled=True)
    await status.delete()
    await _show_menu_reply(message)


async def process_title_template(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    clear_state(user_id)
    text = ve.sanitize_title(message.text or "")
    if not text:
        await message.reply_text("⚠️ القالب فاضي. ابعتي نص.")
        return
    if len(text) > MAX_TITLE_TEMPLATE:
        await message.reply_text(f"⚠️ القالب طويل. الحد الأقصى {MAX_TITLE_TEMPLATE} حرف.")
        return
    await db.update_video_settings(user_id, title_template=text)
    await _show_menu_reply(message)


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #
def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:video$"))
    async def menu_video_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await _show_menu_edit(cq)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^video:wm_toggle$"))
    async def wm_toggle_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        if not wm_configured(vs):
            await cq.answer("اضبطي نص أو لوجو للعلامة الأول.", show_alert=True)
            return
        await db.update_video_settings(user_id, wm_enabled=not vs.wm_enabled)
        await _show_menu_edit(cq)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^video:wm_text$"))
    async def wm_text_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_VIDEO_WM_TEXT)
        await cq.message.edit_text(
            "✏️ ابعتي نص العلامة المائية (لحد 3 سطور، عربي أو إنجليزي).\n\n" + EMOJI_NOTE,
            reply_markup=cancel_kb(),
            parse_mode=_PM,
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^video:wm_image$"))
    async def wm_image_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_VIDEO_WM_IMAGE)
        await cq.message.edit_text(
            "🖼 ابعتي صورة اللوجو اللي تتحط على الفيديو.\n\n" + LOGO_TIP.replace("**", ""),
            reply_markup=cancel_kb(),
            parse_mode=_PM,
        )
        await cq.answer()

    async def _cycle(cq: CallbackQuery, field: str, fn) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        await db.update_video_settings(user_id, **{field: fn(getattr(vs, field))})
        await _show_menu_edit(cq)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^video:wm_pos$"))
    async def wm_pos_cb(client: Client, cq: CallbackQuery) -> None:
        await _cycle(cq, "wm_position", ve.next_position)

    @app.on_callback_query(filters.regex(r"^video:wm_size$"))
    async def wm_size_cb(client: Client, cq: CallbackQuery) -> None:
        await _cycle(cq, "wm_size", ve.next_size)

    @app.on_callback_query(filters.regex(r"^video:wm_opacity$"))
    async def wm_opacity_cb(client: Client, cq: CallbackQuery) -> None:
        await _cycle(cq, "wm_opacity", ve.next_opacity)

    @app.on_callback_query(filters.regex(r"^video:wm_show$"))
    async def wm_show_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        if not wm_configured(vs):
            await cq.answer("اضبطي نص أو لوجو للعلامة الأول.", show_alert=True)
            return
        await cq.answer("جارٍ تجهيز المعاينة…")
        path = os.path.join(config.DOWNLOAD_DIR, f"wm_preview_{user_id}_{int(time.time())}.jpg")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, ve.render_preview, spec_from_settings(vs), path
            )
            await client.send_photo(
                cq.message.chat.id, path, caption="👁 معاينة العلامة المائية (إطار تجريبي)"
            )
        except Exception as exc:
            logger.exception("Preview failed for user %s", user_id)
            await cq.message.reply_text(f"❌ فشلت المعاينة: {exc}")
        finally:
            if os.path.exists(path):
                os.remove(path)

    @app.on_callback_query(filters.regex(r"^video:wm_del$"))
    async def wm_del_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        if vs.wm_image_path and os.path.exists(vs.wm_image_path):
            try:
                os.remove(vs.wm_image_path)
            except OSError:
                pass
        await db.update_video_settings(
            user_id, wm_enabled=False, wm_text=None, wm_image_path=None, wm_mode="text"
        )
        await _show_menu_edit(cq)
        await cq.answer("اتحذفت العلامة")

    @app.on_callback_query(filters.regex(r"^video:title_tpl$"))
    async def title_tpl_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        set_state(user_id, UserState.AWAITING_VIDEO_TITLE_TEMPLATE)
        rows = [[Btn("❌ إلغاء", callback_data="cancel")]]
        if vs.title_template:
            rows.insert(0, [Btn("🗑 مسح القالب", callback_data="video:title_tpl_del")])
        await cq.message.edit_text(
            "🏷 ابعتي العنوان الافتراضي اللي يتحط على أي فيديو.\n"
            "تقدري تكتبي {filename} وهيتبدّل باسم الملف (من غير الامتداد).\n"
            "مثال: {filename} | الكارفة\n\n"
            "تقدري برضه تحطي عنوان مختلف لفيديو معيّن من الأزرار اللي تحته.",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=_PM,
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^video:title_tpl_del$"))
    async def title_tpl_del_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await db.update_video_settings(cq.from_user.id, title_template=None)
        await _show_menu_edit(cq)
        await cq.answer("اتمسح القالب")

    @app.on_callback_query(filters.regex(r"^video:title_overlay$"))
    async def title_overlay_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        vs = await db.get_video_settings(user_id)
        await db.update_video_settings(user_id, title_overlay=not vs.title_overlay)
        await _show_menu_edit(cq)
        await cq.answer()
