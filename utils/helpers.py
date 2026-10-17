"""
utils/helpers.py
==================
Small, dependency-light utility functions shared across handlers:
* Human-readable byte formatting
* Media duration extraction (via `hachoir`)
* Caption template rendering
* Filename sanitisation
"""

import os
import re
from typing import Optional

from hachoir.metadata import extractMetadata
from hachoir.parser import createParser


def human_bytes(size: float) -> str:
    """Convert a byte count into a human-readable string (e.g. '120.5 MB')."""
    if size is None:
        return "0 B"
    power = 1024.0
    n = 0
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(size)
    while size >= power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"


def human_time(seconds: float) -> str:
    """Convert seconds into a compact H:MM:SS / M:SS style string."""
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


MAX_NAME_CHARS = 200
MAX_CAPTION = 1024

# Extensions the user may type on purpose to change a file's extension.
_KNOWN_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".rtf",
    ".zip", ".rar", ".7z", ".epub", ".apk", ".json", ".html",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".3gp",
    ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
}


def sanitize_filename(name: str) -> str:
    """Strip characters that are illegal or problematic in filenames.

    Long names are shortened WITHOUT cutting the extension off (a file whose
    extension was truncated would not open).
    """
    name = (name or "").strip()
    # Remove path separators and other filesystem-unsafe characters.
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)
    # Collapse whitespace; trailing dots/spaces are not valid on every system.
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if len(name) > MAX_NAME_CHARS:
        stem, ext = os.path.splitext(name)
        if len(ext) > 15:  # not a real extension, just a dot inside a long title
            stem, ext = name, ""
        name = stem[: MAX_NAME_CHARS - len(ext)].rstrip() + ext
    return name


def ensure_extension(new_name: str, original_name: str) -> str:
    """
    Make sure the renamed file keeps a usable extension.

    "Lecture 1.5 intro" has a dot but no extension, so the original one
    (.pdf) is appended. A different but well-known extension typed on
    purpose (".docx") is respected.
    """
    orig_ext = file_extension(original_name)
    if not orig_ext or not new_name:
        return new_name
    new_ext = file_extension(new_name).lower()
    if new_ext == orig_ext.lower() or new_ext in _KNOWN_EXTS:
        return new_name
    return new_name + orig_ext


def default_file_name(message_id: int, mime_type: str, media_type: str) -> str:
    """A name with a proper extension for media Telegram sends without one."""
    import mimetypes

    ext = mimetypes.guess_extension(mime_type or "") if mime_type else None
    if not ext:
        ext = {"video": ".mp4", "audio": ".mp3"}.get(media_type, "")
    return f"file_{message_id}{ext}"


def get_media_duration(file_path: str) -> Optional[int]:
    """
    Best-effort extraction of a media file's duration in seconds using
    `hachoir`. Returns None if duration can't be determined (e.g. the file
    is not audio/video, or is corrupted).
    """
    parser = None
    try:
        parser = createParser(file_path)
        if parser is None:
            return None
        with parser:
            metadata = extractMetadata(parser)
        if metadata is None:
            return None
        if metadata.has("duration"):
            duration = metadata.get("duration")
            return int(duration.total_seconds())
    except Exception:
        # Metadata extraction is best-effort; never let it crash the pipeline.
        return None
    finally:
        if parser is not None:
            try:
                parser.stream._input.close()
            except Exception:
                pass
    return None


_WORD_JOINER = "\u2060"  # invisible; breaks accidental markdown delimiters


def normalize_link(raw: str) -> Optional[str]:
    """
    Turn what the user typed into a clean https link, or None if it isn't one.
    Accepts https://..., http://..., t.me/..., @username.
    """
    raw = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    if not raw or len(raw) > 300:
        return None
    if raw.startswith("@"):
        name = raw[1:]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
            return f"https://t.me/{name}"
        return None
    low = raw.lower()
    if low.startswith(("t.me/", "telegram.me/", "www.")):
        raw = "https://" + raw
    elif not low.startswith(("https://", "http://")):
        return None
    if not re.fullmatch(r"https?://[^\s]+\.[^\s]+", raw):
        return None
    return raw.replace("(", "%28").replace(")", "%29")


def link_markup(text: str, url: str) -> str:
    """
    `text` as a clickable link ("Create link") in Pyrogram's markdown. Characters of
    `text` that would be read as markup are neutralised so the link can't break.
    """
    safe = text.replace("[", "［").replace("]", "］").replace("`", "'")
    for ch in ("_", "*", "~", "-", "|"):
        safe = safe.replace(ch * 2, ch + _WORD_JOINER + ch)
        safe = safe.replace(ch * 2, ch + _WORD_JOINER + ch)  # odd runs (___)
    return f"[{safe}]({url})"


def render_caption(
    template: Optional[str],
    filename: str,
    filesize: int,
    duration: Optional[int] = None,
    link: Optional[str] = None,
) -> str:
    """
    Fill a user/channel caption template with runtime values.

    Supported placeholders: {filename}, {filesize}, {duration}
    Falls back to a sensible default caption if no template is set.
    If `link` is given the file name inside the caption becomes a clickable
    link to it (the bot's or the user's channel). In the default caption the
    file name is the last line of the message.
    """
    duration_str = human_time(duration) if duration else "غير متاح"

    def build(name_text: str, linked: bool = False) -> str:
        if not template:
            # Default layout: details first, the file title LAST (under everything),
            # as a clickable link when the user set a custom link.
            title = name_text if linked else f"**{name_text}**"
            return f"📦 الحجم: {human_bytes(filesize)}\n\n{title}"
        try:
            return template.format(
                filename=name_text,
                filesize=human_bytes(filesize),
                duration=duration_str,
            )
        except Exception:
            # Unknown placeholder, a stray "{" ... — fail soft.
            return template

    if link:
        text = build(link_markup(filename, link), linked=True)
        if len(text) <= MAX_CAPTION:
            return text
        # Too long once the link markup is added: a cut-off link would show as
        # raw text, so send the plain version instead.
    text = build(filename)
    # Telegram rejects captions over 1024 characters (the whole upload fails).
    return text[:MAX_CAPTION]


def file_extension(filename: str) -> str:
    """Return the file extension including the leading dot, e.g. '.mkv'."""
    _, ext = os.path.splitext(filename)
    return ext
