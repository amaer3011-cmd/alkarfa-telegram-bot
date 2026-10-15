"""
Central configuration for the bot.

Only API_ID, API_HASH and BOT_TOKEN are required from the environment.
Everything else has a built-in default (ADMIN_IDS is optional).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Required Telegram credentials
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")


def _parse_ids(raw: str) -> frozenset:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


# Optional: comma-separated Telegram user ids allowed to manage channels.
# Left empty => everyone can (the previous behaviour).
ADMIN_IDS: frozenset = _parse_ids(os.getenv("ADMIN_IDS", ""))

# Owner(s) of the bot: comma-separated Telegram user ids allowed to open the
# private admin panel (/admin). If OWNER_IDS is empty, ADMIN_IDS is used;
# if both are empty the panel is disabled (nobody is an owner).
OWNER_IDS: frozenset = _parse_ids(os.getenv("OWNER_IDS", "")) or ADMIN_IDS


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


# The "Developer" button on the main menu opens this Telegram profile.
DEVELOPER_USERNAME: str = os.getenv("DEVELOPER_USERNAME", "A7med19_7").lstrip("@")
DEVELOPER_URL: str = f"https://t.me/{DEVELOPER_USERNAME}"

# Time zone used when the admin panel prints dates.
TIMEZONE: str = os.getenv("TIMEZONE", "Africa/Cairo")

# Where everything that must survive a restart lives (database, Telegram
# session, thumbnails, covers, logos). On Railway mount a Volume at /data and
# set DATA_DIR=/data (Railway also sets RAILWAY_VOLUME_MOUNT_PATH for you).
DATA_DIR: str = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
os.makedirs(DATA_DIR, exist_ok=True)

# Internal defaults
DB_URL: str = os.path.join(DATA_DIR, "bot_database.db")
DOWNLOAD_DIR: str = "downloads"  # temporary files only (not persisted)
THUMB_DIR: str = os.path.join(DATA_DIR, "thumbnails")
COVER_DIR: str = os.path.join(DATA_DIR, "covers")
MAX_FILE_SIZE: int = 2 * 1024 * 1024 * 1024  # 2 GB
PROGRESS_EDIT_INTERVAL: float = 3.0
SESSION_NAME: str = "file_renamer_bot"

# How many files may be downloaded/processed/uploaded at the same time.
MAX_CONCURRENT_JOBS: int = 3
# Pyrogram transfer parallelism (default is 1, which serialises every
# upload/download) and the number of update-handler workers.
MAX_CONCURRENT_TRANSMISSIONS: int = 6
UPDATE_WORKERS: int = 16
# Speed: how many 1 MB chunks are requested at once when downloading, and how
# many 512 KB parts are in flight per upload (Kurigram's default is 4).
FAST_DOWNLOAD_WORKERS: int = int(os.getenv("FAST_DOWNLOAD_WORKERS", "8"))
FAST_UPLOAD_WORKERS: int = int(os.getenv("FAST_UPLOAD_WORKERS", "8"))
# Seconds a file waits for the user's decision before it is forgotten.
PENDING_TTL: int = 30 * 60

# ---- Video editing (watermark / title) ---------------------------------
VIDEO_WM_DIR: str = os.path.join(DATA_DIR, "video_wm")  # saved watermark logos, one per user
FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
# Optional path to a .ttf/.otf font used to draw watermark/title text
# (must support Arabic). Empty => look for Noto/DejaVu fonts automatically.
FONT_PATH: str = os.getenv("FONT_PATH", "")
# Re-encoding is CPU heavy: how many videos may be encoded at once.
MAX_CONCURRENT_ENCODES: int = int(os.getenv("MAX_CONCURRENT_ENCODES", "1"))
VIDEO_PRESET: str = os.getenv("VIDEO_PRESET", "veryfast")  # x264 speed/size trade-off
VIDEO_CRF: int = int(os.getenv("VIDEO_CRF", "23"))  # lower = better quality, bigger file
VIDEO_TIMEOUT: int = int(os.getenv("VIDEO_TIMEOUT", str(2 * 3600)))  # seconds per video
TITLE_OVERLAY_SECONDS: int = 5  # how long an on-screen title stays visible

# Create runtime directories automatically.
for _d in (DOWNLOAD_DIR, THUMB_DIR, COVER_DIR, VIDEO_WM_DIR):
    os.makedirs(_d, exist_ok=True)
