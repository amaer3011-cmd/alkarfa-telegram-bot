"""
handlers/guard.py
===================
Runs in handler group -1, i.e. before every other handler:

1. Records / refreshes the user (name, @username, last seen) so the admin
   panel can list them.
2. Blocks banned users.
3. Blocks everybody except the owners while maintenance mode is on.

The owners are never blocked.
"""

import logging
import time

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from database import db

logger = logging.getLogger(__name__)

BANNED_TEXT = "🚫 تم حظرك من استخدام البوت."
MAINTENANCE_TEXT = "🛠 الكارفة في صيانة دلوقتي، جرّب كمان شوية 💕"

# Don't write to the database on every single message of the same user.
_TOUCH_EVERY = 60  # seconds
_last_touch: dict = {}


def maintenance_on() -> bool:
    return db.get_setting("maintenance") == "1"


async def _record(user) -> None:
    now = time.monotonic()
    if now - _last_touch.get(user.id, -_TOUCH_EVERY) < _TOUCH_EVERY:
        return
    _last_touch[user.id] = now
    try:
        await db.touch_user(user.id, user.username, user.first_name)
    except Exception:  # never let bookkeeping break the bot
        logger.exception("Could not record user %s", user.id)


def register(app: Client) -> None:
    @app.on_message(filters.private, group=-1)
    async def guard_message(client: Client, message: Message) -> None:
        user = message.from_user
        if user is None or config.is_owner(user.id):
            return
        await _record(user)
        if db.is_banned(user.id):
            await message.reply_text(BANNED_TEXT)
            message.stop_propagation()
        if maintenance_on():
            await message.reply_text(MAINTENANCE_TEXT)
            message.stop_propagation()

    @app.on_callback_query(group=-1)
    async def guard_callback(client: Client, cq: CallbackQuery) -> None:
        user = cq.from_user
        if user is None or config.is_owner(user.id):
            return
        await _record(user)
        if db.is_banned(user.id):
            await cq.answer(BANNED_TEXT, show_alert=True)
            cq.stop_propagation()
        if maintenance_on():
            await cq.answer(MAINTENANCE_TEXT, show_alert=True)
            cq.stop_propagation()
