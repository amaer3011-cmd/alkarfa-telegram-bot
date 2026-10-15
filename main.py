"""
main.py
========
Application entry point. Initialises the database, constructs the
Pyrogram Client, registers every handler module, and starts polling.

Run with:
    python main.py
"""

import asyncio
import logging

from pyrogram import Client

import config
from utils import button_colors, fast_transfer
from database.db import init_db
from handlers import (
    admin, caption, channel, common, cover, guard, inbox, rename, start, thumbnail, video,
    watermark,
)

button_colors.install()  # colour every inline button (needs Kurigram)
fast_transfer.patch_upload_workers()  # more parallel upload parts per file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
# Pyrogram is chatty at INFO level; keep it at WARNING unless debugging.
logging.getLogger("pyrogram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def build_client() -> Client:
    app = Client(
        config.SESSION_NAME,
        workdir=config.DATA_DIR,  # keeps the .session file on the volume
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=config.UPDATE_WORKERS,
        max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS,
    )

    # Callback-query handlers use disjoint "prefix:" patterns so their
    # registration order doesn't matter. handlers/common.py owns the only
    # catch-all text/photo message handlers and must be registered after
    # the feature modules it depends on (thumbnail/caption/channel/rename)
    # since it imports their processing functions at module load time.
    # guard runs in handler group -1: it records users and blocks banned
    # users / maintenance mode before any other handler sees the update.
    guard.register(app)
    admin.register(app)
    inbox.register(app)  # user replies in an owner<->user chat
    start.register(app)
    thumbnail.register(app)
    caption.register(app)
    watermark.register(app)
    video.register(app)
    cover.register(app)
    channel.register(app)
    rename.register(app)
    common.register(app)

    return app


async def _startup() -> None:
    if not (config.API_ID and config.API_HASH and config.BOT_TOKEN):
        raise RuntimeError(
            "API_ID, API_HASH and BOT_TOKEN must be set (env vars or .env file)."
        )
    await init_db()
    logger.info("Database initialised.")
    if not config.OWNER_IDS:
        logger.warning("OWNER_IDS is empty: the admin panel is disabled for everybody.")


def main() -> None:
    # NOTE: we deliberately don't use `asyncio.run(_startup())` here. It
    # creates its own event loop, runs `_startup`, and then *closes and
    # unsets* that loop when done. This Pyrogram version's `Dispatcher`
    # still calls the legacy `asyncio.get_event_loop()` synchronously
    # inside `Client.__init__` (invoked below via `build_client()`), and
    # on Python 3.10+ that raises `RuntimeError: There is no current
    # event loop in thread 'MainThread'` once no loop has been left set
    # as current — exactly the crash-loop seen in production.
    #
    # Fix: create one event loop ourselves, set it as current, and keep
    # it open (don't close it) across both `_startup()` and `app.run()`,
    # which is itself just a thin `loop.run_until_complete(...)` wrapper
    # and will happily reuse this same loop.
    try:  # uvloop is a drop-in, noticeably faster event loop (Linux/macOS)
        import uvloop

        loop = uvloop.new_event_loop()
        logger.info("Using uvloop.")
    except Exception:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_startup())
        app = build_client()
        logger.info("Starting bot...")
        app.run()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
