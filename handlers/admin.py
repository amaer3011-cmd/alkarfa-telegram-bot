"""
handlers/admin.py
===================
The owner's private control panel ("👑 لوحة الأدمن").

Only users listed in OWNER_IDS can open it (`/admin` or the button in the
main menu). Everything is button-driven, like the rest of the bot:

* 📊 statistics
* 👥 paginated user list + user card (ban / unban / send a message)
* 🔍 search a user by id or @username
* 🚫 / ✅ ban and unban by id
* 📣 broadcast a message to every user
* 🛠 maintenance mode (only owners can use the bot)
* 💬 change the /start message
* 📢 channel management (the existing channel menu)
* 📄 export the user list as CSV
"""

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    InputUserDeactivated,
    MessageNotModified,
    PeerIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    Message,
)

import config
from database import db
from handlers.guard import maintenance_on
from utils.keyboards import cancel_kb
from utils.sender import spawn, with_flood_retry
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)

# User-controlled text (names, usernames, broadcast text) is shown as plain
# text so a name like "*x_" can never break the message formatting.
_PM = ParseMode.DISABLED
PAGE_SIZE = 8


# --------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------- #
async def _owner_check(_, __, update) -> bool:
    user = getattr(update, "from_user", None)
    return bool(user and config.is_owner(user.id))


owner_only = filters.create(_owner_check)


# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #
def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return timezone.utc


def _fmt(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, _tz()).strftime("%Y-%m-%d %H:%M")


def _name(p: db.UserProfile) -> str:
    return (p.first_name or "").strip() or "بدون اسم"


async def _edit(cq: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=kb, parse_mode=_PM)
    except MessageNotModified:
        pass


async def _resolve_id(text: str) -> Optional[int]:
    text = (text or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    profile = await db.find_profile(text)
    return profile.user_id if profile else None


# --------------------------------------------------------------------- #
# Keyboards / screens
# --------------------------------------------------------------------- #
def home_kb() -> InlineKeyboardMarkup:
    maint = "🛠 وضع الصيانة: شغّال ✅" if maintenance_on() else "🛠 وضع الصيانة: مقفول ⚪️"
    return InlineKeyboardMarkup(
        [
            [Btn("📊 الإحصائيات", callback_data="admin:stats"),
             Btn("👥 المستخدمين", callback_data="admin:users:0")],
            [Btn("🔍 بحث عن مستخدم", callback_data="admin:find")],
            [Btn("🚫 حظر", callback_data="admin:banask"),
             Btn("✅ فك حظر", callback_data="admin:unbanask")],
            [Btn("📣 إذاعة للكل", callback_data="admin:bc")],
            [Btn(maint, callback_data="admin:maint")],
            [Btn("💬 تغيير رسالة /start", callback_data="admin:welcome")],
            [Btn("📢 إدارة القنوات", callback_data="menu:channel")],
            [Btn("📄 تصدير المستخدمين (CSV)", callback_data="admin:export")],
            [Btn("🔙 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


HOME_TEXT = "👑 لوحة تحكم الكارفة\n\nاختاري اللي عايزاه:"


def _back_kb(target: str = "admin:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("🔙 رجوع", callback_data=target)]])


def _card_kb(p: db.UserProfile) -> InlineKeyboardMarkup:
    ban_btn = (
        Btn("✅ فك الحظر", callback_data=f"admin:ban:{p.user_id}")
        if p.is_banned
        else Btn("🚫 حظر", callback_data=f"admin:ban:{p.user_id}")
    )
    return InlineKeyboardMarkup(
        [
            [ban_btn, Btn("💬 ابعتله رسالة", callback_data=f"admin:dm:{p.user_id}")],
            [Btn("🔙 قائمة المستخدمين", callback_data="admin:users:0")],
        ]
    )


def _card_text(p: db.UserProfile) -> str:
    return (
        f"👤 {_name(p)}\n"
        f"🆔 {p.user_id}\n"
        f"🔗 {'@' + p.username if p.username else '—'}\n"
        f"📅 انضم: {_fmt(p.date_joined)}\n"
        f"🕐 آخر نشاط: {_fmt(p.last_seen)}\n"
        f"📁 ملفات اتعدّلت: {p.files_count}\n"
        f"الحالة: {'🚫 محظور' if p.is_banned else '✅ نشط'}"
    )


async def _show_card(cq: CallbackQuery, user_id: int) -> None:
    p = await db.get_profile(user_id)
    if p is None:
        await cq.answer("المستخدم ده مش موجود.", show_alert=True)
        return
    await _edit(cq, _card_text(p), _card_kb(p))


# --------------------------------------------------------------------- #
# Text / photo flows (dispatched from handlers/common.py by state)
# --------------------------------------------------------------------- #
async def process_user_query(client: Client, message: Message) -> None:
    clear_state(message.from_user.id)
    p = await db.find_profile(message.text or "")
    if p is None:
        await message.reply_text("⚠️ مفيش مستخدم بالمعرّف ده.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    await message.reply_text(_card_text(p), reply_markup=_card_kb(p), parse_mode=_PM)


async def _ban_flow(message: Message, banned: bool) -> None:
    clear_state(message.from_user.id)
    target = await _resolve_id(message.text or "")
    if target is None:
        await message.reply_text("⚠️ مش فاهمة المعرّف. ابعتي الـ ID أو @username.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    if banned and config.is_owner(target):
        await message.reply_text("😅 مينفعش تحظري نفسك أو أدمن تاني.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    await db.set_banned(target, banned)
    await message.reply_text(
        f"{'🚫 تم حظر' if banned else '✅ تم فك حظر'} {target}.", reply_markup=_back_kb(), parse_mode=_PM
    )


async def process_ban_id(client: Client, message: Message) -> None:
    await _ban_flow(message, True)


async def process_unban_id(client: Client, message: Message) -> None:
    await _ban_flow(message, False)


async def process_dm(client: Client, message: Message) -> None:
    uid = message.from_user.id
    target = get_state(uid).data.get("target")
    clear_state(uid)
    if target is None:
        return
    try:
        await with_flood_retry(lambda: message.copy(target))
    except Exception as exc:
        await message.reply_text(f"❌ ماقدرتش أبعتله: {exc}", reply_markup=_back_kb(), parse_mode=_PM)
        return
    await message.reply_text("✅ اتبعتت.", reply_markup=_back_kb(f"admin:u:{target}"), parse_mode=_PM)


async def process_welcome_text(client: Client, message: Message) -> None:
    clear_state(message.from_user.id)
    if not message.text:
        await message.reply_text("⚠️ ابعتي نص بس.", reply_markup=_back_kb("admin:welcome"), parse_mode=_PM)
        return
    # `.markdown` keeps any bold/italic/links she typed.
    await db.set_setting("welcome_text", message.text.markdown)
    await message.reply_text("✅ اتغيّرت رسالة /start.", reply_markup=_back_kb("admin:welcome"), parse_mode=_PM)


# ---- broadcast ------------------------------------------------------- #
_bc_pending: dict = {}
_bc_running = False


async def process_broadcast(client: Client, message: Message) -> None:
    uid = message.from_user.id
    clear_state(uid)
    _bc_pending[uid] = message
    count = len(await db.broadcast_ids())
    kb = InlineKeyboardMarkup(
        [
            [Btn(f"✅ ابعت لـ {count} مستخدم", callback_data="admin:bc_go")],
            [Btn("❌ إلغاء", callback_data="admin:bc_cancel")],
        ]
    )
    await message.reply_text(
        f"📣 الرسالة اللي فوق هتتبعت لـ {count} مستخدم (غير المحظورين). تأكيد؟",
        reply_markup=kb,
        parse_mode=_PM,
    )


async def _run_broadcast(client: Client, msg: Message, ids: list, status: Message) -> None:
    global _bc_running
    _bc_running = True
    sent = blocked = failed = 0
    total = len(ids)
    try:
        for i, target in enumerate(ids, 1):
            try:
                await with_flood_retry(lambda t=target: msg.copy(t))
                sent += 1
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                blocked += 1
            except Exception:
                logger.exception("Broadcast to %s failed", target)
                failed += 1
            if i % 25 == 0:
                try:
                    await status.edit_text(f"⏳ جارٍ الإرسال… {i}/{total}", parse_mode=_PM)
                except Exception:
                    pass
            await asyncio.sleep(0.05)  # stay well under Telegram's ~30 msg/s limit
    finally:
        _bc_running = False
    try:
        await status.edit_text(
            f"✅ خلصت الإذاعة\n\n📨 اتبعتت: {sent}\n🚷 موقفين/حاظرين البوت: {blocked}\n⚠️ فشلت: {failed}",
            reply_markup=_back_kb(),
            parse_mode=_PM,
        )
    except Exception:
        pass


# ---- export ---------------------------------------------------------- #
async def _export_users(client: Client, chat_id: int) -> None:
    profiles = await db.all_profiles()
    path = os.path.join(config.DOWNLOAD_DIR, f"users_{int(time.time())}.csv")
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["user_id", "username", "name", "joined", "last_seen", "files", "banned"])
            for p in profiles:
                writer.writerow(
                    [p.user_id, p.username or "", p.first_name or "", _fmt(p.date_joined),
                     _fmt(p.last_seen), p.files_count, int(p.is_banned)]
                )
        await client.send_document(chat_id, path, caption=f"📄 {len(profiles)} مستخدم")
    finally:
        if os.path.exists(path):
            os.remove(path)


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #
def register(app: Client) -> None:
    @app.on_message(filters.command("admin") & filters.private & owner_only)
    async def admin_cmd(client: Client, message: Message) -> None:
        clear_state(message.from_user.id)
        await message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode=_PM)

    @app.on_callback_query(owner_only & filters.regex(r"^admin:home$"))
    async def home_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await _edit(cq, HOME_TEXT, home_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:stats$"))
    async def stats_cb(client: Client, cq: CallbackQuery) -> None:
        s = await db.get_stats()
        text = (
            "📊 الإحصائيات\n\n"
            f"👥 كل المستخدمين: {s['total']}\n"
            f"🆕 جداد آخر 24 ساعة: {s['new_24h']}\n"
            f"🗓 جداد آخر 7 أيام: {s['new_7d']}\n"
            f"🟢 نشطين آخر 24 ساعة: {s['active_24h']}\n"
            f"🚫 محظورين: {s['banned']}\n"
            f"📁 ملفات اتعدّلت: {s['files']}\n"
            f"📢 قنوات مربوطة: {s['channels']}\n"
            f"🛠 الصيانة: {'شغّالة' if maintenance_on() else 'مقفولة'}"
        )
        await _edit(cq, text, _back_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:users:\d+$"))
    async def users_cb(client: Client, cq: CallbackQuery) -> None:
        page = int(cq.data.split(":")[2])
        total = (await db.get_stats())["total"]
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(page, pages - 1)
        profiles = await db.list_profiles(page * PAGE_SIZE, PAGE_SIZE)
        rows = []
        for p in profiles:
            label = f"{'🚫 ' if p.is_banned else ''}{_name(p)[:20]} • {p.user_id}"
            rows.append([Btn(label, callback_data=f"admin:u:{p.user_id}")])
        nav = []
        if page > 0:
            nav.append(Btn("◀️ السابق", callback_data=f"admin:users:{page - 1}"))
        if page < pages - 1:
            nav.append(Btn("التالي ▶️", callback_data=f"admin:users:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Btn("🔙 رجوع", callback_data="admin:home")])
        await _edit(cq, f"👥 المستخدمين ({total}) — صفحة {page + 1}/{pages}", InlineKeyboardMarkup(rows))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:u:-?\d+$"))
    async def user_card_cb(client: Client, cq: CallbackQuery) -> None:
        await _show_card(cq, int(cq.data.split(":")[2]))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:ban:-?\d+$"))
    async def ban_toggle_cb(client: Client, cq: CallbackQuery) -> None:
        target = int(cq.data.split(":")[2])
        if config.is_owner(target):
            await cq.answer("مينفعش تحظري أدمن.", show_alert=True)
            return
        await db.set_banned(target, not db.is_banned(target))
        await _show_card(cq, target)
        await cq.answer("تم ✅")

    @app.on_callback_query(owner_only & filters.regex(r"^admin:dm:-?\d+$"))
    async def dm_cb(client: Client, cq: CallbackQuery) -> None:
        target = int(cq.data.split(":")[2])
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_DM, target=target)
        await _edit(cq, "💬 ابعتي الرسالة (نص أو صورة) اللي عايزة توصله.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:find$"))
    async def find_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_USER_QUERY)
        await _edit(cq, "🔍 ابعتي الـ ID أو @username بتاع المستخدم.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:banask$"))
    async def banask_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_BAN_ID)
        await _edit(cq, "🚫 ابعتي الـ ID أو @username اللي عايزة تحظريه.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:unbanask$"))
    async def unbanask_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_UNBAN_ID)
        await _edit(cq, "✅ ابعتي الـ ID أو @username اللي عايزة تفكي حظره.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:maint$"))
    async def maint_cb(client: Client, cq: CallbackQuery) -> None:
        await db.set_setting("maintenance", None if maintenance_on() else "1")
        await _edit(cq, HOME_TEXT, home_kb())
        await cq.answer("وضع الصيانة شغّال ✅" if maintenance_on() else "وضع الصيانة اتقفل")

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bc$"))
    async def bc_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_BROADCAST)
        await _edit(cq, "📣 ابعتي الرسالة اللي عايزة تذيعيها لكل المستخدمين (نص أو صورة).", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bc_cancel$"))
    async def bc_cancel_cb(client: Client, cq: CallbackQuery) -> None:
        _bc_pending.pop(cq.from_user.id, None)
        await _edit(cq, "❌ اتلغت الإذاعة.", _back_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bc_go$"))
    async def bc_go_cb(client: Client, cq: CallbackQuery) -> None:
        if _bc_running:
            await cq.answer("في إذاعة شغّالة دلوقتي، استني تخلص.", show_alert=True)
            return
        msg = _bc_pending.pop(cq.from_user.id, None)
        if msg is None:
            await cq.answer("انتهت صلاحية الطلب. ابعتي الرسالة تاني.", show_alert=True)
            return
        ids = await db.broadcast_ids()
        await _edit(cq, f"⏳ جارٍ الإرسال… 0/{len(ids)}", None)
        spawn(_run_broadcast(client, msg, ids, cq.message))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:welcome$"))
    async def welcome_cb(client: Client, cq: CallbackQuery) -> None:
        from handlers.start import welcome_text

        kb = InlineKeyboardMarkup(
            [
                [Btn("✏️ تغيير الرسالة", callback_data="admin:welcome_set")],
                [Btn("♻️ رجوع للرسالة الأصلية", callback_data="admin:welcome_reset")],
                [Btn("🔙 رجوع", callback_data="admin:home")],
            ]
        )
        await _edit(cq, "💬 رسالة /start الحالية:\n\n" + welcome_text(), kb)
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:welcome_set$"))
    async def welcome_set_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_WELCOME)
        await _edit(cq, "✏️ ابعتي نص رسالة /start الجديدة.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:welcome_reset$"))
    async def welcome_reset_cb(client: Client, cq: CallbackQuery) -> None:
        await db.set_setting("welcome_text", None)
        await _edit(cq, "♻️ رجعت الرسالة الأصلية.", _back_kb("admin:welcome"))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:export$"))
    async def export_cb(client: Client, cq: CallbackQuery) -> None:
        await cq.answer("جارٍ تجهيز الملف…")
        await _export_users(client, cq.message.chat.id)
