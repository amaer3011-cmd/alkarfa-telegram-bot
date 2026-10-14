"""
handlers/inbox.py
===================
The user's side of an owner <-> user conversation.

When the owner opens a chat with a member from the admin panel and sends a
message, a "thread" is opened for that member. From then on his plain
messages (text, photos, voice notes, stickers…) are forwarded to the owner —
with a "💬 رد" button — until the owner ends the chat (or 72 hours pass with
no messages). Files, videos and audio keep going to the normal editing flow,
and so does anything the member sends while he is in the middle of a bot
flow (setting a thumbnail, naming a file…).
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import config
from database import db
from handlers.guard import describe_message
from utils.sender import with_flood_retry
from utils.state import UserState, get_state

logger = logging.getLogger(__name__)


async def _relay_check(_, __, message: Message) -> bool:
    user = message.from_user
    if user is None or config.is_owner(user.id):
        return False
    if not db.thread_alive(user.id):
        return False
    if message.text and message.text.startswith("/"):
        return False
    if message.document or message.video or message.audio:
        return False  # the file-editing flow owns these
    return get_state(user.id).state == UserState.NONE


relay = filters.create(_relay_check)


def register(app: Client) -> None:
    @app.on_message(filters.private & relay)
    async def relay_to_owner(client: Client, message: Message) -> None:
        uid = message.from_user.id
        owner = db.thread_owner(uid)
        if owner is None:
            return
        db.touch_thread(uid)
        await db.log_event(uid, "chat_in", describe_message(message))

        owner_state = get_state(owner)
        in_chat = (
            owner_state.state == UserState.ADMIN_CHAT and owner_state.data.get("target") == uid
        )
        try:
            if not in_chat:
                u = message.from_user
                who = (u.first_name or "").strip() or "بدون اسم"
                if u.username:
                    who += f" (@{u.username})"
                kb = InlineKeyboardMarkup(
                    [[Btn("💬 رد", callback_data=f"admin:chat:{uid}"),
                      Btn("👤 البروفايل", callback_data=f"admin:u:{uid}")]]
                )
                await client.send_message(
                    owner, f"💬 رسالة من {who} • {uid}", reply_markup=kb, parse_mode=ParseMode.DISABLED
                )
            await with_flood_retry(lambda: message.copy(owner))
        except Exception:
            logger.exception("Could not relay a message of user %s to owner %s", uid, owner)
