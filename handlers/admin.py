"""
handlers/admin.py
===================
The owner's private control panel ("👑 لوحة الأدمن").

Only users listed in OWNER_IDS can open it (`/admin` or the button in the
main menu). Everything is button-driven, like the rest of the bot:

* 📊 statistics
* 👥 clear user list (filters: active / newest / banned / muted / restricted)
  and a full user profile: details, files, activity log, conversation log
* 🔍 search a user by id, @username or part of the name
* 🚫 ban (permanent or for a period) — the user gets the "not free" message
* 🔇 mute (the bot ignores the user completely for a period)
* 🔒 restrict (no files / no videos / no PDFs / no settings / daily limit)
* 💬 chat with a user from inside the bot (he can reply)
* 📁 every file the bot edited for the members, with a button to get the file
* 📣 broadcast, 🛠 maintenance, welcome / ban message, channels, CSV export
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
from handlers.guard import DEFAULT_BAN_TEXT, ban_text, describe_message, maintenance_on
from utils import ui
from utils.durations import human, parse_duration
from utils.helpers import human_bytes
from utils.keyboards import cancel_kb
from utils.sender import spawn, with_flood_retry
from utils.state import UserState, clear_state, get_state, set_state

logger = logging.getLogger(__name__)

# User-controlled text (names, usernames, broadcast text) is shown as plain
# text so a name like "*x_" can never break the message formatting.
_PM = ParseMode.DISABLED
PAGE_SIZE = 8       # users / files per page
LOG_PAGE_SIZE = 12  # log lines per page

_FLAG_LABELS = {
    "files": "منع إرسال أي ملفات",
    "video": "منع الفيديو",
    "pdf": "منع ملفات PDF",
    "settings": "منع تغيير الإعدادات",
}
_FLAG_SHORT = {"files": "الملفات", "video": "الفيديو", "pdf": "PDF", "settings": "الإعدادات"}
_FLAG_KEYS = {"f": "files", "v": "video", "p": "pdf", "s": "settings"}
_FLAG_CODES = {v: k for k, v in _FLAG_KEYS.items()}

_KIND_ICONS = {
    "msg": "💬", "cb": "🔘", "file_in": "📥", "file_done": "✅", "file_fail": "❌",
    "blocked": "⛔️", "admin": "👑", "chat_in": "⬅️", "chat_out": "➡️",
}
_MEDIA_ICONS = {"video": "🎬", "audio": "🎵", "document": "📄"}

_DURATIONS = (
    (900, "15 دقيقة"), (3600, "ساعة"), (6 * 3600, "6 ساعات"), (86400, "يوم"),
    (3 * 86400, "3 أيام"), (7 * 86400, "أسبوع"), (30 * 86400, "شهر"),
)


# --------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------- #
async def _owner_check(_, __, update) -> bool:
    user = getattr(update, "from_user", None)
    return bool(user and config.is_owner(user.id))


owner_only = filters.create(_owner_check)


async def _chat_mode_check(_, __, message) -> bool:
    """The owner is in chat mode and this is not a command."""
    user = message.from_user
    if not user or not config.is_owner(user.id):
        return False
    if get_state(user.id).state != UserState.ADMIN_CHAT:
        return False
    return not (message.text and message.text.startswith("/"))


chat_mode = filters.create(_chat_mode_check)


# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #
def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return timezone.utc


def _fmt(ts: Optional[int], short: bool = False) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, _tz()).strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")


def _remaining(until: Optional[int]) -> str:
    if not until:
        return "دايمًا"
    return f"لحد {_fmt(until)} (فاضل {human(max(60, int(until - time.time())))})"


def _name(p) -> str:
    return (getattr(p, "first_name", None) or "").strip() or "بدون اسم"


def _label(uid: int, first_name: Optional[str], username: Optional[str]) -> str:
    base = (first_name or "").strip() or "بدون اسم"
    return f"{base} (@{username})" if username else f"{base} ({uid})"


def _icons(p: db.UserProfile) -> str:
    icons = ""
    if p.is_banned:
        icons += "🚫"
    if p.is_muted:
        icons += "🔇"
    if p.is_restricted:
        icons += "🔒"
    if not icons:
        icons = "🟢" if p.last_seen and time.time() - p.last_seen < 600 else "⚪️"
    return icons


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


def _uid_of(cq: CallbackQuery, idx: int = 2) -> int:
    return int(cq.data.split(":")[idx])


def _pager(prefix: str, page: int, pages: int) -> list:
    nav = []
    if page > 0:
        nav.append(Btn("◀️ السابق", callback_data=f"{prefix}:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("التالي ▶️", callback_data=f"{prefix}:{page + 1}"))
    return nav


def _pages(total: int, size: int) -> int:
    return max(1, -(-total // size))


# --------------------------------------------------------------------- #
# Keyboards / screens
# --------------------------------------------------------------------- #
def home_kb() -> InlineKeyboardMarkup:
    maint = "🛠 وضع الصيانة: شغّال ✅" if maintenance_on() else "🛠 وضع الصيانة: مقفول ⚪️"
    return InlineKeyboardMarkup(
        [
            [Btn("📊 الإحصائيات", callback_data="admin:stats"),
             Btn("👥 المستخدمين", callback_data="admin:ul:a:0")],
            [Btn("🔍 بحث عن مستخدم", callback_data="admin:find")],
            [Btn("📁 كل الملفات المعدّلة", callback_data="admin:fa:0")],
            [Btn("🚫 حظر", callback_data="admin:banask"),
             Btn("✅ فك حظر", callback_data="admin:unbanask")],
            [Btn("📣 إذاعة للكل", callback_data="admin:bc")],
            [Btn(maint, callback_data="admin:maint")],
            [Btn("💬 رسالة /start", callback_data="admin:welcome"),
             Btn("✏️ رسالة الحظر", callback_data="admin:bantext")],
            [Btn("📢 إدارة القنوات", callback_data="menu:channel")],
            [Btn("📄 تصدير المستخدمين (CSV)", callback_data="admin:export")],
            [Btn("🏠 القائمة الرئيسية", callback_data="menu:main")],
        ]
    )


HOME_TEXT = ui.plain_header("👑", "لوحة تحكم الكارفة") + "\n✨ اختاري اللي عايزاه من الأزرار 👇"


def _back_kb(target: str = "admin:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("🔙 رجوع", callback_data=target)]])


def _card_kb(p: db.UserProfile) -> InlineKeyboardMarkup:
    uid = p.user_id
    ban_btn = (
        Btn("✅ فك الحظر", callback_data=f"admin:unban:{uid}")
        if p.is_banned
        else Btn("🚫 حظر", callback_data=f"admin:ban:{uid}")
    )
    mute_btn = (
        Btn("🔊 فك الكتم", callback_data=f"admin:unmute:{uid}")
        if p.is_muted
        else Btn("🔇 كتم / إيقاف", callback_data=f"admin:mute:{uid}")
    )
    return InlineKeyboardMarkup(
        [
            [ban_btn, mute_btn],
            [Btn("🔒 تقييد الاستخدام", callback_data=f"admin:rest:{uid}"),
             Btn("💬 محادثة", callback_data=f"admin:chat:{uid}")],
            [Btn("📁 ملفاته", callback_data=f"admin:fl:{uid}:0"),
             Btn("📜 سجله مع البوت", callback_data=f"admin:log:{uid}:0")],
            [Btn("🗂 سجل المحادثة", callback_data=f"admin:chlog:{uid}:0")],
            [Btn("🔄 تحديث", callback_data=f"admin:u:{uid}"),
             Btn("🔙 المستخدمين", callback_data="admin:ul:a:0")],
        ]
    )


async def _card_text(p: db.UserProfile) -> str:
    count, total_bytes, last_file = await db.file_stats(p.user_id)
    today = await db.files_today(p.user_id)
    lines = [
        ui.plain_header("👤", _name(p)).rstrip("\n"),
        "",
        f"🆔 المعرّف ‹ {p.user_id}",
        f"🔗 اليوزر ‹ {'@' + p.username if p.username else '—'}",
        f"📅 انضم ‹ {_fmt(p.date_joined)}",
        f"🕐 آخر نشاط ‹ {_fmt(p.last_seen)}",
        ui.THIN,
        f"📁 ملفات اتعدّلت ‹ {max(count, p.files_count)} (النهاردة: {today})"
        + (f" — {human_bytes(total_bytes)}" if total_bytes else ""),
        f"📅 آخر ملف ‹ {_fmt(last_file)}",
        ui.THIN,
    ]
    if p.is_banned:
        lines.append(f"🚫 محظور {_remaining(p.banned_until)}")
    if p.is_muted:
        lines.append(f"🔇 مكتوم {_remaining(p.muted_until if p.muted_until < 4000000000 else 0)}")
    if p.is_restricted:
        parts = [f"منع {_FLAG_SHORT[f]}" for f in db.RESTRICT_FLAGS if f in p.restrict_flags]
        if p.daily_limit:
            parts.append(f"حد يومي {p.daily_limit} ملف")
        lines.append("🔒 تقييدات: " + "، ".join(parts))
    if db.thread_owner(p.user_id):
        lines.append("💬 في محادثة مفتوحة معاه")
    if not (p.is_banned or p.is_muted or p.is_restricted):
        lines.append("الحالة: ✅ نشط")
    return "\n".join(lines)


async def _show_card(cq: CallbackQuery, user_id: int) -> None:
    p = await db.get_profile(user_id)
    if p is None:
        await cq.answer("المستخدم ده مش موجود.", show_alert=True)
        return
    await _edit(cq, await _card_text(p), _card_kb(p))


def _dur_kb(action: str, uid: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for secs, label in _DURATIONS:
        row.append(Btn(label, callback_data=f"admin:{action}:{uid}:{secs}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [Btn("⏳ مدة تانية", callback_data=f"admin:{action}c:{uid}"),
         Btn("♾ دايمًا", callback_data=f"admin:{action}:{uid}:0")]
    )
    rows.append([Btn("🔙 رجوع", callback_data=f"admin:u:{uid}")])
    return InlineKeyboardMarkup(rows)


_DUR_TEXT = {
    "ban": "🚫 حظر {name}\n\nاختاري المدة — بعدها يرجع تلقائي:\n(هتتبعتله رسالة «مش فاضيالك» فورًا)",
    "mute": "🔇 كتم {name}\n\nالبوت مش هيرد عليه خالص كأنه واقف.\nاختاري المدة:",
}


# --------------------------------------------------------------------- #
# Penalty actions
# --------------------------------------------------------------------- #
async def _apply_ban(client: Client, uid: int, secs: int) -> str:
    until = int(time.time()) + secs if secs else None
    await db.set_banned(uid, True, until)
    await db.log_event(uid, "admin", f"🚫 حظر {human(secs)}")
    try:  # tell him right away
        await client.send_message(uid, ban_text(), parse_mode=_PM)
    except Exception:
        pass
    return f"🚫 اتحظر {human(secs)}"


async def _apply_mute(uid: int, secs: int) -> str:
    until = int(time.time()) + secs if secs else 0
    await db.set_muted(uid, until)
    await db.log_event(uid, "admin", f"🔇 كتم {human(secs)}")
    return f"🔇 اتكتم {human(secs)}"


# --------------------------------------------------------------------- #
# Text / photo flows (dispatched from handlers/common.py by state)
# --------------------------------------------------------------------- #
def _results_kb(profiles: list) -> InlineKeyboardMarkup:
    rows = [
        [Btn(f"{_icons(p)} {_name(p)[:18]} • {p.user_id}", callback_data=f"admin:u:{p.user_id}")]
        for p in profiles
    ]
    rows.append([Btn("🔙 رجوع", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


async def process_user_query(client: Client, message: Message) -> None:
    clear_state(message.from_user.id)
    found = await db.search_profiles(message.text or "")
    if not found:
        await message.reply_text("⚠️ مفيش مستخدم بالمعرّف ده.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    if len(found) == 1:
        p = found[0]
        await message.reply_text(await _card_text(p), reply_markup=_card_kb(p), parse_mode=_PM)
        return
    await message.reply_text(
        f"🔍 لقيت {len(found)} مستخدم:", reply_markup=_results_kb(found), parse_mode=_PM
    )


async def _ban_flow(message: Message, banned: bool) -> None:
    clear_state(message.from_user.id)
    target = await _resolve_id(message.text or "")
    if target is None:
        await message.reply_text("⚠️ مش فاهمة المعرّف. ابعتي الـ ID أو @username.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    if banned and config.is_owner(target):
        await message.reply_text("😅 مينفعش تحظري نفسك أو أدمن تاني.", reply_markup=_back_kb(), parse_mode=_PM)
        return
    if banned:
        p = await db.get_profile(target)
        await message.reply_text(
            _DUR_TEXT["ban"].format(name=_name(p) if p else target),
            reply_markup=_dur_kb("ban", target),
            parse_mode=_PM,
        )
        return
    await db.set_banned(target, False)
    await db.log_event(target, "admin", "✅ فك الحظر")
    await message.reply_text(f"✅ تم فك حظر {target}.", reply_markup=_back_kb(f"admin:u:{target}"), parse_mode=_PM)


async def process_ban_id(client: Client, message: Message) -> None:
    await _ban_flow(message, True)


async def process_unban_id(client: Client, message: Message) -> None:
    await _ban_flow(message, False)


async def process_duration(client: Client, message: Message) -> None:
    """The owner typed a custom ban / mute length."""
    uid = message.from_user.id
    entry = get_state(uid)
    target, action = entry.data.get("target"), entry.data.get("action")
    if target is None or action not in ("ban", "mute"):
        clear_state(uid)
        return
    secs = parse_duration(message.text or "")
    if secs is None:
        await message.reply_text(
            "⚠️ مش فاهمة المدة. اكتبي مثلًا: 30m أو 2h أو 3d أو 1w "
            "(أو: ساعتين، 3 ايام، أسبوع، دايم).",
            reply_markup=cancel_kb(),
            parse_mode=_PM,
        )
        return
    clear_state(uid)
    if action == "ban":
        if config.is_owner(target):
            await message.reply_text("😅 مينفعش تحظري أدمن.", reply_markup=_back_kb(), parse_mode=_PM)
            return
        await _apply_ban(client, target, secs)
    else:
        await _apply_mute(target, secs)
    p = await db.get_profile(target)
    if p:
        await message.reply_text(await _card_text(p), reply_markup=_card_kb(p), parse_mode=_PM)
    else:
        await message.reply_text("✅ تم.", reply_markup=_back_kb(), parse_mode=_PM)


async def process_limit(client: Client, message: Message) -> None:
    uid = message.from_user.id
    target = get_state(uid).data.get("target")
    text = (message.text or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if target is None:
        clear_state(uid)
        return
    if not text.isdigit():
        await message.reply_text("⚠️ ابعتي رقم بس (0 = من غير حد).", reply_markup=cancel_kb(), parse_mode=_PM)
        return
    clear_state(uid)
    flags, _old = db.get_restrictions(target)
    await db.set_restrictions(target, flags, int(text))
    await db.log_event(target, "admin", f"🔒 الحد اليومي = {int(text) or 'من غير حد'}")
    await message.reply_text(
        "✅ اتحفظ.", reply_markup=_back_kb(f"admin:rest:{target}"), parse_mode=_PM
    )


async def process_ban_text(client: Client, message: Message) -> None:
    clear_state(message.from_user.id)
    if not message.text:
        await message.reply_text("⚠️ ابعتي نص بس.", reply_markup=_back_kb("admin:bantext"), parse_mode=_PM)
        return
    await db.set_setting("ban_text", message.text)
    await message.reply_text("✅ اتغيّرت رسالة الحظر.", reply_markup=_back_kb("admin:bantext"), parse_mode=_PM)


async def process_dm(client: Client, message: Message) -> None:
    """Kept for the (rare) single-message flow; the main way is chat mode."""
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


# ---- chat with a user -------------------------------------------------- #
def _chat_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [Btn("🚪 خروج (ردوده لسه توصلني)", callback_data=f"admin:chatexit:{uid}")],
            [Btn("⏹ إنهاء المحادثة نهائيًا", callback_data=f"admin:chatend:{uid}")],
            [Btn("👤 البروفايل", callback_data=f"admin:u:{uid}")],
        ]
    )


async def process_chat(client: Client, message: Message) -> None:
    """Every message the owner sends in chat mode is copied to the user."""
    owner = message.from_user.id
    target = get_state(owner).data.get("target")
    if target is None:
        clear_state(owner)
        return
    try:
        await with_flood_retry(lambda: message.copy(target))
    except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
        await message.reply_text("🚷 العضو ده موقف البوت أو حسابه مش متاح — الرسالة ماوصلتش.", parse_mode=_PM)
        return
    except Exception as exc:
        await message.reply_text(f"❌ الرسالة ماوصلتش: {exc}", parse_mode=_PM)
        return
    await db.open_thread(target, owner)
    await db.log_event(target, "chat_out", describe_message(message))
    try:  # tiny "delivered" tick
        await message.react("👍")
    except Exception:
        pass


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
            writer.writerow(["user_id", "username", "name", "joined", "last_seen", "files",
                             "banned", "muted", "restricted"])
            for p in profiles:
                writer.writerow(
                    [p.user_id, p.username or "", p.first_name or "", _fmt(p.date_joined),
                     _fmt(p.last_seen), p.files_count, int(p.is_banned), int(p.is_muted),
                     int(p.is_restricted)]
                )
        await client.send_document(chat_id, path, caption=f"📄 {len(profiles)} مستخدم")
    finally:
        if os.path.exists(path):
            os.remove(path)


# --------------------------------------------------------------------- #
# Files / log screens
# --------------------------------------------------------------------- #
def _file_title(f: db.FileRecord) -> str:
    return f"{_MEDIA_ICONS.get(f.media_type or '', '📄')} {(f.new_name or f.orig_name or '—')}"


async def _files_screen(cq: CallbackQuery, user_id: Optional[int], page: int) -> None:
    total = await db.count_files(user_id)
    pages = _pages(total, PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    files = await db.list_files(user_id, page * PAGE_SIZE, PAGE_SIZE)
    rows = []
    for f in files:
        owner = f"  👤{_name(f)[:10]}" if user_id is None else ""
        rows.append(
            [Btn(f"{_file_title(f)[:34]}{owner}", callback_data=f"admin:f:{f.id}")]
        )
    base = "admin:fa" if user_id is None else f"admin:fl:{user_id}"
    nav = _pager(base, page, pages)
    if nav:
        rows.append(nav)
    rows.append([Btn("🔙 رجوع", callback_data="admin:home" if user_id is None else f"admin:u:{user_id}")])
    head = "📁 كل الملفات المعدّلة" if user_id is None else f"📁 ملفات {user_id}"
    text = f"{head} ({total}) — صفحة {page + 1}/{pages}\nالأحدث فوق. اضغطي على ملف لتفاصيله واستلامه."
    if total == 0:
        text = f"{head}\n\nمفيش ملفات لسه."
    await _edit(cq, text, InlineKeyboardMarkup(rows))


async def _log_screen(cq: CallbackQuery, uid: int, page: int, kinds, title: str, base: str) -> None:
    total = await db.count_events(uid, kinds)
    pages = _pages(total, LOG_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = await db.get_events(uid, kinds, page * LOG_PAGE_SIZE, LOG_PAGE_SIZE)
    lines = [f"{title} ({total}) — صفحة {page + 1}/{pages}", ""]
    for ts, kind, detail in rows:
        if kind == "chat_in":
            who = "⬅️ هو"
        elif kind == "chat_out":
            who = "➡️ إنت"
        else:
            who = _KIND_ICONS.get(kind, "•")
        lines.append(f"{_fmt(ts, short=True)} {who} {(detail or '')[:110]}")
    if not rows:
        lines.append("مفيش حاجة لسه.")
    kb_rows = []
    nav = _pager(base, page, pages)
    if nav:
        kb_rows.append(nav)
    kb_rows.append([Btn("🔙 البروفايل", callback_data=f"admin:u:{uid}")])
    await _edit(cq, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


def _rest_kb(uid: int) -> InlineKeyboardMarkup:
    flags, limit = db.get_restrictions(uid)
    rows = []
    for flag in db.RESTRICT_FLAGS:
        mark = "✅" if flag in flags else "⬜️"
        rows.append(
            [Btn(f"{mark} {_FLAG_LABELS[flag]}", callback_data=f"admin:rt:{uid}:{_FLAG_CODES[flag]}")]
        )
    rows.append(
        [Btn(f"🔢 حد الملفات في اليوم: {limit if limit else 'من غير حد'}", callback_data=f"admin:rlim:{uid}")]
    )
    rows.append([Btn("🧹 شيل كل التقييدات", callback_data=f"admin:rclr:{uid}")])
    rows.append([Btn("🔙 رجوع", callback_data=f"admin:u:{uid}")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #
def register(app: Client) -> None:
    # Chat mode first: it must see the owner's messages (text, media, files)
    # before the file-renaming handlers do.
    @app.on_message(filters.private & chat_mode)
    async def chat_msg(client: Client, message: Message) -> None:
        await process_chat(client, message)

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
            ui.plain_header("📊", "الإحصائيات")
            + f"\n👥 كل المستخدمين ‹ {s['total']}\n"
            f"🟢 نشطين آخر 24 ساعة ‹ {s['active_24h']}\n"
            f"🆕 جداد آخر 24 ساعة ‹ {s['new_24h']}\n"
            f"🗓 جداد آخر 7 أيام ‹ {s['new_7d']}\n"
            f"{ui.THIN}\n"
            f"📁 ملفات اتعدّلت ‹ {s['files']}\n"
            f"📢 قنوات مربوطة ‹ {s['channels']}\n"
            f"{ui.THIN}\n"
            f"🚫 محظورين ‹ {s['banned']}\n"
            f"🔇 مكتومين ‹ {s['muted']}\n"
            f"🔒 مقيّدين ‹ {s['restricted']}\n"
            f"🛠 الصيانة ‹ {'شغّالة 🔴' if maintenance_on() else 'مقفولة 🟢'}"
        )
        await _edit(cq, text, _back_kb())
        await cq.answer()

    # ---- user list ----------------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:ul:[a-z]:\d+$"))
    async def users_cb(client: Client, cq: CallbackQuery) -> None:
        _, _, flt, page_s = cq.data.split(":")
        if flt not in db.USER_FILTERS:
            flt = "a"
        total = await db.count_profiles(flt)
        pages = _pages(total, PAGE_SIZE)
        page = max(0, min(int(page_s), pages - 1))
        profiles = await db.list_profiles(page * PAGE_SIZE, PAGE_SIZE, flt)

        tabs = (("a", "الكل"), ("o", "🟢 نشطين"), ("n", "🆕 الجداد"),
                ("b", "🚫 محظورين"), ("m", "🔇 مكتومين"), ("r", "🔒 مقيّدين"))
        tab_rows, row = [], []
        for code, label in tabs:
            row.append(Btn(("• " if code == flt else "") + label, callback_data=f"admin:ul:{code}:0"))
            if len(row) == 3:
                tab_rows.append(row)
                row = []
        rows = tab_rows + [
            [Btn(f"{_icons(p)} {_name(p)[:16]} • {'@' + p.username if p.username else p.user_id}",
                 callback_data=f"admin:u:{p.user_id}")]
            for p in profiles
        ]
        nav = _pager(f"admin:ul:{flt}", page, pages)
        if nav:
            rows.append(nav)
        rows.append([Btn("🔍 بحث", callback_data="admin:find"), Btn("🔙 رجوع", callback_data="admin:home")])
        titles = {"a": "كل المستخدمين (الأحدث نشاطًا أولًا)", "o": "النشطين آخر 24 ساعة",
                  "n": "الأحدث انضمامًا", "b": "المحظورين", "m": "المكتومين", "r": "المقيّدين"}
        text = (
            f"👥 {titles[flt]} — {total}\nصفحة {page + 1}/{pages}\n"
            "🟢 أونلاين  ⚪️ عادي  🚫 محظور  🔇 مكتوم  🔒 مقيّد"
        )
        await _edit(cq, text, InlineKeyboardMarkup(rows))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:u:-?\d+$"))
    async def user_card_cb(client: Client, cq: CallbackQuery) -> None:
        await _show_card(cq, _uid_of(cq))
        await cq.answer()

    # ---- ban ------------------------------------------------------------ #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:ban:-?\d+$"))
    async def ban_menu_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        if config.is_owner(target):
            await cq.answer("مينفعش تحظري أدمن.", show_alert=True)
            return
        p = await db.get_profile(target)
        await _edit(cq, _DUR_TEXT["ban"].format(name=_name(p) if p else target), _dur_kb("ban", target))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:ban:-?\d+:\d+$"))
    async def ban_go_cb(client: Client, cq: CallbackQuery) -> None:
        target, secs = _uid_of(cq), int(cq.data.split(":")[3])
        if config.is_owner(target):
            await cq.answer("مينفعش تحظري أدمن.", show_alert=True)
            return
        note = await _apply_ban(client, target, secs)
        await _show_card(cq, target)
        await cq.answer(note)

    @app.on_callback_query(owner_only & filters.regex(r"^admin:banc:-?\d+$"))
    async def ban_custom_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_DURATION, target=_uid_of(cq), action="ban")
        await _edit(
            cq,
            "⏳ اكتبي مدة الحظر:\n30m = 30 دقيقة • 2h = ساعتين • 3d = 3 أيام • 1w = أسبوع\n"
            "(أو بالعربي: ساعتين، 3 ايام، أسبوع — وللأبد: دايم)",
            cancel_kb(),
        )
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:unban:-?\d+$"))
    async def unban_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        await db.set_banned(target, False)
        await db.log_event(target, "admin", "✅ فك الحظر")
        await _show_card(cq, target)
        await cq.answer("اتفك الحظر ✅")

    # ---- mute ----------------------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:mute:-?\d+$"))
    async def mute_menu_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        if config.is_owner(target):
            await cq.answer("مينفعش تكتمي أدمن.", show_alert=True)
            return
        p = await db.get_profile(target)
        await _edit(cq, _DUR_TEXT["mute"].format(name=_name(p) if p else target), _dur_kb("mute", target))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:mute:-?\d+:\d+$"))
    async def mute_go_cb(client: Client, cq: CallbackQuery) -> None:
        target, secs = _uid_of(cq), int(cq.data.split(":")[3])
        if config.is_owner(target):
            await cq.answer("مينفعش تكتمي أدمن.", show_alert=True)
            return
        note = await _apply_mute(target, secs)
        await _show_card(cq, target)
        await cq.answer(note)

    @app.on_callback_query(owner_only & filters.regex(r"^admin:mutec:-?\d+$"))
    async def mute_custom_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_DURATION, target=_uid_of(cq), action="mute")
        await _edit(
            cq,
            "⏳ اكتبي مدة الكتم:\n30m = 30 دقيقة • 2h = ساعتين • 3d = 3 أيام • 1w = أسبوع\n"
            "(أو بالعربي: ساعتين، 3 ايام، أسبوع — وللأبد: دايم)",
            cancel_kb(),
        )
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:unmute:-?\d+$"))
    async def unmute_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        await db.set_muted(target, None)
        await db.log_event(target, "admin", "🔊 فك الكتم")
        await _show_card(cq, target)
        await cq.answer("اتفك الكتم ✅")

    # ---- restrictions --------------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:rest:-?\d+$"))
    async def rest_cb(client: Client, cq: CallbackQuery) -> None:
        uid = _uid_of(cq)
        p = await db.get_profile(uid)
        await _edit(
            cq,
            f"🔒 تقييد استخدام {_name(p) if p else uid}\n\nاضغطي على أي خيار لتشغيله / إيقافه:",
            _rest_kb(uid),
        )
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:rt:-?\d+:[fvps]$"))
    async def rest_toggle_cb(client: Client, cq: CallbackQuery) -> None:
        _, _, uid_s, code = cq.data.split(":")
        uid, flag = int(uid_s), _FLAG_KEYS[code]
        flags, limit = db.get_restrictions(uid)
        flags = set(flags)
        flags.symmetric_difference_update({flag})
        await db.set_restrictions(uid, flags, limit)
        await db.log_event(
            uid, "admin", ("🔒 " if flag in flags else "🔓 ") + _FLAG_LABELS[flag]
        )
        await _edit(cq, cq.message.text or "🔒 تقييد", _rest_kb(uid))
        await cq.answer("تم ✅")

    @app.on_callback_query(owner_only & filters.regex(r"^admin:rlim:-?\d+$"))
    async def rest_limit_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_LIMIT, target=_uid_of(cq))
        await _edit(cq, "🔢 ابعتي أقصى عدد ملفات في اليوم (0 = من غير حد).", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:rclr:-?\d+$"))
    async def rest_clear_cb(client: Client, cq: CallbackQuery) -> None:
        uid = _uid_of(cq)
        await db.set_restrictions(uid, (), 0)
        await db.log_event(uid, "admin", "🔓 اتشالت كل التقييدات")
        await _edit(cq, "🔒 تقييد الاستخدام\n\nاتشالت كل التقييدات ✅", _rest_kb(uid))
        await cq.answer()

    # ---- chat ----------------------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:chat:-?\d+$"))
    async def chat_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        p = await db.get_profile(target)
        set_state(cq.from_user.id, UserState.ADMIN_CHAT, target=target)
        who = _label(target, p.first_name if p else None, p.username if p else None)
        text = (
            f"💬 إنتي دلوقتي في محادثة مع {who}\n\n"
            "أي رسالة تبعتيها (نص، صورة، صوت، ملف…) هتوصله زي ما هي، "
            "ورده هيجيلك هنا.\n"
            "للخروج من وضع المحادثة: زر 🚪 تحت أو ابعتي /admin."
        )
        await _edit(cq, text, _chat_kb(target))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:chatexit:-?\d+$"))
    async def chat_exit_cb(client: Client, cq: CallbackQuery) -> None:
        clear_state(cq.from_user.id)
        await _show_card(cq, _uid_of(cq))
        await cq.answer("خرجتي من المحادثة — ردوده هتوصلك برضو.")

    @app.on_callback_query(owner_only & filters.regex(r"^admin:chatend:-?\d+$"))
    async def chat_end_cb(client: Client, cq: CallbackQuery) -> None:
        target = _uid_of(cq)
        clear_state(cq.from_user.id)
        await db.close_thread(target)
        await _show_card(cq, target)
        await cq.answer("اتقفلت المحادثة ✅")

    # ---- files and logs ------------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:fa:\d+$"))
    async def files_all_cb(client: Client, cq: CallbackQuery) -> None:
        await _files_screen(cq, None, int(cq.data.split(":")[2]))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:fl:-?\d+:\d+$"))
    async def files_user_cb(client: Client, cq: CallbackQuery) -> None:
        await _files_screen(cq, _uid_of(cq), int(cq.data.split(":")[3]))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:f:\d+$"))
    async def file_detail_cb(client: Client, cq: CallbackQuery) -> None:
        f = await db.get_file(_uid_of(cq))
        if f is None:
            await cq.answer("الملف ده مش موجود في السجل.", show_alert=True)
            return
        kinds = {"video": "🎬 فيديو", "audio": "🎵 صوت", "document": "📄 مستند"}
        text = (
            f"{_file_title(f)}\n\n"
            f"✏️ الاسم الأصلي: {f.orig_name or '—'}\n"
            f"النوع: {kinds.get(f.media_type or '', f.media_type or '—')}\n"
            f"الحجم: {human_bytes(f.size) if f.size else '—'}\n"
            f"👤 بتاع: {_label(f.user_id, f.first_name, f.username)}\n"
            f"🆔 {f.user_id}\n"
            f"📅 {_fmt(f.ts)}"
        )
        if f.notes:
            text += f"\n\n{f.notes}"
        kb = InlineKeyboardMarkup(
            [
                [Btn("📥 استلام الملف", callback_data=f"admin:fg:{f.id}")],
                [Btn("👤 صاحبه", callback_data=f"admin:u:{f.user_id}"),
                 Btn("📁 ملفاته", callback_data=f"admin:fl:{f.user_id}:0")],
                [Btn("🔙 كل الملفات", callback_data="admin:fa:0")],
            ]
        )
        await _edit(cq, text, kb)
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:fg:\d+$"))
    async def file_get_cb(client: Client, cq: CallbackQuery) -> None:
        f = await db.get_file(_uid_of(cq))
        if f is None or not f.file_id:
            await cq.answer("الملف مش متاح للاستلام.", show_alert=True)
            return
        await cq.answer("جارٍ الإرسال…")
        caption = f"📁 {f.new_name or ''}\n👤 {_label(f.user_id, f.first_name, f.username)} • 🆔 {f.user_id}"
        send = {"video": client.send_video, "audio": client.send_audio}.get(
            f.media_type or "", client.send_document
        )
        try:
            await with_flood_retry(
                lambda: send(cq.message.chat.id, f.file_id, caption=caption, parse_mode=_PM)
            )
        except Exception as exc:
            await client.send_message(
                cq.message.chat.id, f"❌ ماقدرتش أبعت الملف: {exc}", parse_mode=_PM
            )

    @app.on_callback_query(owner_only & filters.regex(r"^admin:log:-?\d+:\d+$"))
    async def log_cb(client: Client, cq: CallbackQuery) -> None:
        await _log_screen(
            cq, _uid_of(cq), int(cq.data.split(":")[3]), None,
            "📜 سجله مع البوت", f"admin:log:{_uid_of(cq)}",
        )
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:chlog:-?\d+:\d+$"))
    async def chlog_cb(client: Client, cq: CallbackQuery) -> None:
        await _log_screen(
            cq, _uid_of(cq), int(cq.data.split(":")[3]), ("chat_in", "chat_out"),
            "🗂 سجل المحادثة", f"admin:chlog:{_uid_of(cq)}",
        )
        await cq.answer()

    # ---- search / ban by id --------------------------------------------- #
    @app.on_callback_query(owner_only & filters.regex(r"^admin:find$"))
    async def find_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_USER_QUERY)
        await _edit(cq, "🔍 ابعتي الـ ID أو @username أو جزء من الاسم.", cancel_kb())
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

    # ---- maintenance / broadcast / texts / export ------------------------ #
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

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bantext$"))
    async def bantext_cb(client: Client, cq: CallbackQuery) -> None:
        kb = InlineKeyboardMarkup(
            [
                [Btn("✏️ تغيير الرسالة", callback_data="admin:bantext_set")],
                [Btn("♻️ رجوع للرسالة الأصلية", callback_data="admin:bantext_reset")],
                [Btn("🔙 رجوع", callback_data="admin:home")],
            ]
        )
        await _edit(cq, "🚫 رسالة الحظر الحالية (بتتبعت للمحظور):\n\n" + ban_text(), kb)
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bantext_set$"))
    async def bantext_set_cb(client: Client, cq: CallbackQuery) -> None:
        set_state(cq.from_user.id, UserState.AWAITING_ADMIN_BANTEXT)
        await _edit(cq, "✏️ ابعتي نص رسالة الحظر الجديدة.", cancel_kb())
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:bantext_reset$"))
    async def bantext_reset_cb(client: Client, cq: CallbackQuery) -> None:
        await db.set_setting("ban_text", None)
        await _edit(cq, "♻️ رجعت رسالة الحظر الأصلية:\n\n" + DEFAULT_BAN_TEXT, _back_kb("admin:bantext"))
        await cq.answer()

    @app.on_callback_query(owner_only & filters.regex(r"^admin:export$"))
    async def export_cb(client: Client, cq: CallbackQuery) -> None:
        await cq.answer("جارٍ تجهيز الملف…")
        await _export_users(client, cq.message.chat.id)
