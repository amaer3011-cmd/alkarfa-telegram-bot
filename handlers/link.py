"""
handlers/link.py
==================
The "🔗 الرابط المخصص" menu.

The user saves one link (the bot's or their channel's). From then on, the file
name inside every caption the bot sends becomes a clickable link to it —
Telegram's "Create link" — automatically, for any name they type. The same link
is used by the channels the user bound.

The Telegram *file name* itself can't hold a link; the link lives in the caption.
"""

import logging
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from database import db
from utils.helpers import normalize_link, render_caption
from utils import ui
from utils.keyboards import cancel_kb, link_menu_kb
from utils.state import UserState, clear_state, set_state

logger = logging.getLogger(__name__)

_bot_link: Optional[str] = None

_SAMPLE_NAME = "اسم الملف.pdf"


async def bot_link(client: Client) -> Optional[str]:
    """https://t.me/<this bot's username> (cached)."""
    global _bot_link
    if _bot_link is None:
        try:
            me = await client.get_me()
            if me.username:
                _bot_link = f"https://t.me/{me.username}"
        except Exception:
            logger.exception("Could not read the bot's own username")
    return _bot_link


def _preview(url: str) -> str:
    return render_caption(None, _SAMPLE_NAME, 1024 * 1024, link=url)


def _menu_text(url: Optional[str]) -> str:
    text = (
        ui.header("🔗", "الرابط المخصص")
        + "\nاحفظ رابط البوت أو قناتك، و**اسم أي ملف** يوصلك هيظهر في التسمية "
        "كـ **لينك قابل للضغط** تلقائيًا.\n\n"
    )
    if url:
        text += f"✅ الرابط الحالي:\n`{url}`\n\nشكل الاسم في الملف:\n{_preview(url)}"
    else:
        text += "الرابط الحالي: غير مضبوط."
    return text


async def process_link_text(client: Client, message: Message) -> None:
    """state == AWAITING_CUSTOM_LINK."""
    user_id = message.from_user.id
    url = normalize_link(message.text or "")
    if url is None:
        await message.reply_text(
            "⚠️ ده مش رابط صالح. ابعت رابط زي `https://t.me/اسم_القناة` أو `@اسم_القناة`.",
            reply_markup=cancel_kb(),
        )
        return
    clear_state(user_id)
    await db.set_user_link(user_id, url)
    await message.reply_text(
        "✅ تم حفظ الرابط. شكل اسم الملف في التسمية التوضيحية:\n\n" + _preview(url),
        reply_markup=link_menu_kb(True)
    )


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^menu:link$"))
    async def menu_link_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        url = user.custom_link if user else None
        await cq.message.edit_text(
            _menu_text(url), reply_markup=link_menu_kb(bool(url))
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^link:bot$"))
    async def link_bot_cb(client: Client, cq: CallbackQuery) -> None:
        url = await bot_link(client)
        if not url:
            await cq.answer("تعذّر معرفة رابط البوت. ابعت الرابط بنفسك.", show_alert=True)
            return
        await db.ensure_user(cq.from_user.id)
        await db.set_user_link(cq.from_user.id, url)
        await cq.message.edit_text(
            "✅ تم ضبط رابط البوت.\n\n" + _menu_text(url),
            reply_markup=link_menu_kb(True)
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^link:set$"))
    async def link_set_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_CUSTOM_LINK)
        await cq.message.edit_text(
            "📢 ابعت رابط قناتك أو أي رابط تحبه.\n\n"
            "أمثلة:\n`https://t.me/اسم_القناة`\n`@اسم_القناة`",
            reply_markup=cancel_kb(),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^link:show$"))
    async def link_show_cb(client: Client, cq: CallbackQuery) -> None:
        user = await db.get_user(cq.from_user.id)
        if not user or not user.custom_link:
            await cq.answer("مفيش رابط محفوظ.", show_alert=True)
            return
        await cq.message.edit_text(
            _menu_text(user.custom_link),
            reply_markup=link_menu_kb(True)
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^link:del$"))
    async def link_del_cb(client: Client, cq: CallbackQuery) -> None:
        await db.set_user_link(cq.from_user.id, None)
        await cq.message.edit_text(
            "🗑 تم حذف الرابط. اسم الملف هيرجع نص عادي في التسمية التوضيحية.",
            reply_markup=link_menu_kb(False),
        )
        await cq.answer()
