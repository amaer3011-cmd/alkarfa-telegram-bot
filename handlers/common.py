"""
handlers/common.py
====================
Singletons shared by the whole bot:

1. The generic "❌ إلغاء" callback used by every multi-step flow.
2. The only catch-all text and photo routers. All state-based dispatch
   happens here; feature modules expose plain async processing functions.
3. `route_image`, which sends a photo (or an image sent as a file) to the
   flow the user is currently waiting in.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from handlers import admin, caption, channel, cover, rename, thumbnail, video, watermark
from utils.keyboards import cancel_kb
from utils.state import UserState, clear_state, get_state

logger = logging.getLogger(__name__)

_TEXT_HANDLERS = {
    UserState.AWAITING_FILENAME: rename.process_filename,
    UserState.AWAITING_CAPTION_TEXT: caption.process_caption_text,
    UserState.AWAITING_WATERMARK_PATTERNS: watermark.process_watermark_patterns,
    UserState.AWAITING_CHANNEL_BIND_ID: channel.process_channel_bind_id,
    UserState.AWAITING_CHANNEL_UNBIND_ID: channel.process_channel_unbind_id,
    UserState.AWAITING_CHANNEL_THUMB_ID: channel.process_channel_thumb_id,
    UserState.AWAITING_CHANNEL_CAPTION_ID: channel.process_channel_caption_id,
    UserState.AWAITING_CHANNEL_CAPTION_TEXT: channel.process_channel_caption_text,
    UserState.AWAITING_CHANNEL_WATERMARK_ID: channel.process_channel_watermark_id,
    UserState.AWAITING_CHANNEL_WATERMARK_PATTERNS: channel.process_channel_watermark_patterns,
    UserState.AWAITING_CHANNEL_COVER_ID: channel.process_channel_cover_id,
    UserState.AWAITING_CHANNEL_DELCOVER_ID: channel.process_channel_delcover_id,
    UserState.AWAITING_ADMIN_USER_QUERY: admin.process_user_query,
    UserState.AWAITING_ADMIN_BAN_ID: admin.process_ban_id,
    UserState.AWAITING_ADMIN_UNBAN_ID: admin.process_unban_id,
    UserState.AWAITING_ADMIN_BROADCAST: admin.process_broadcast,
    UserState.AWAITING_ADMIN_WELCOME: admin.process_welcome_text,
    UserState.AWAITING_ADMIN_DM: admin.process_dm,
    UserState.AWAITING_VIDEO_WM_TEXT: video.process_wm_text,
    UserState.AWAITING_VIDEO_TITLE_TEMPLATE: video.process_title_template,
    UserState.AWAITING_FILE_VIDEO_TITLE: rename.process_file_video_title,
}

# state -> function that consumes the image the user sends next
_IMAGE_HANDLERS = {
    UserState.AWAITING_THUMB_PHOTO: thumbnail.process_thumb_photo,
    UserState.AWAITING_CHANNEL_THUMB_PHOTO: channel.process_channel_thumb_photo,
    UserState.AWAITING_COVER_PHOTO: cover.process_cover_photo,
    UserState.AWAITING_CHANNEL_COVER_PHOTO: channel.process_channel_cover_photo,
    UserState.AWAITING_FILE_COVER: rename.process_file_cover,
    UserState.AWAITING_VIDEO_WM_IMAGE: video.process_wm_image,
    # the owner may broadcast / DM a photo too
    UserState.AWAITING_ADMIN_BROADCAST: admin.process_broadcast,
    UserState.AWAITING_ADMIN_DM: admin.process_dm,
}


async def route_image(client: Client, message: Message) -> bool:
    """Hand the image to the flow the user is waiting in. False if none."""
    fn = _IMAGE_HANDLERS.get(get_state(message.from_user.id).state)
    if fn is None:
        return False
    await fn(client, message)
    return True


def register(app: Client) -> None:
    rename.image_document_hook = route_image

    @app.on_callback_query(filters.regex(r"^cancel$"))
    async def generic_cancel_cb(client: Client, cq: CallbackQuery) -> None:
        user_id = cq.from_user.id
        clear_state(user_id)
        rename.clear_pending(user_id)
        await cq.message.edit_text("❌ تم الإلغاء.")
        await cq.answer()

    @app.on_message(filters.private & filters.text)
    async def text_router(client: Client, message: Message) -> None:
        if message.text.startswith("/"):
            return
        state = get_state(message.from_user.id).state
        handler_fn = _TEXT_HANDLERS.get(state)
        if handler_fn is not None:
            await handler_fn(client, message)
        elif state in _IMAGE_HANDLERS or state == UserState.AWAITING_THUMB_CONFIRM:
            await message.reply_text("📸 أنا أنتظر صورة، أرسلها أو اضغط إلغاء.", reply_markup=cancel_kb())

    @app.on_message(filters.private & filters.photo)
    async def photo_router(client: Client, message: Message) -> None:
        if not await route_image(client, message):
            await thumbnail.offer_thumb_confirmation(client, message)
