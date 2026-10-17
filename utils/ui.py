"""
utils/ui.py
=============
The bot's visual language, in one place. Every screen uses the same
divider, the same headers and the same progress bar so the whole bot
looks like one polished product.

Only direction-neutral characters are used (no box-drawing corners) so
the layout stays clean in right-to-left Arabic text.
"""

DIVIDER = "━━━━━━━━━━━━━━━━━━"
THIN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

_FILLED = "▰"
_EMPTY = "▱"


def bar(percentage: float, length: int = 12) -> str:
    """▰▰▰▰▰▱▱▱▱▱▱▱ — a smooth, modern progress bar."""
    percentage = max(0.0, min(100.0, percentage or 0.0))
    filled = int(length * percentage / 100)
    return _FILLED * filled + _EMPTY * (length - filled)


def header(emoji: str, title: str) -> str:
    """✦ title block used at the top of every screen."""
    return f"{emoji} **{title}**\n{DIVIDER}\n"


def screen(emoji: str, title: str, body: str = "", footer: str = "") -> str:
    """A complete screen: header + body (+ footer line)."""
    text = header(emoji, title)
    if body:
        text += "\n" + body.strip("\n") + "\n"
    if footer:
        text += f"\n{THIN}\n{footer}"
    return text.rstrip("\n")


def menu(emoji: str, title: str, hint: str = "اختر من الأزرار بالأسفل 👇") -> str:
    return screen(emoji, title, footer=f"✨ {hint}")


def row(emoji: str, label: str, value: str) -> str:
    """One tidy info line:  📦 الحجم ‹ 12 MB"""
    return f"{emoji} **{label}** ‹ {value}"


def success(extra: str = "") -> str:
    text = header("✅", "تمت العملية بنجاح") + "\n💕 الملف وصلك جاهز ومضبوط\n✨ ابعتلي أي ملف تاني وسيب الباقي عليا"
    if extra:
        text += f"\n\n{THIN}\n{extra}"
    return text


def card(stage_emoji: str, action: str, percentage: float, lines: str = "") -> str:
    """Live progress card shown while downloading / uploading / encoding."""
    first = action.lstrip()[:1]
    has_emoji = bool(first) and ord(first) > 0x2000  # action already starts with an emoji
    prefix = "" if has_emoji else f"{stage_emoji} "
    text = (
        f"{prefix}**{action}**\n{DIVIDER}\n\n"
        f"{bar(percentage)}  `{percentage:.0f}%`\n"
    )
    if lines:
        text += f"\n{lines}"
    return text


def plain_header(emoji: str, title: str) -> str:
    """Same header for screens sent with markdown disabled (admin panel)."""
    return f"{emoji} {title}\n{DIVIDER}\n"


def error(detail: str, hint: str = "جرّب تاني أو ابعت الملف من جديد 💕") -> str:
    """Friendly failure card. `detail` is shown in a code span (markdown-safe)."""
    detail = str(detail).replace("`", "'").replace("\n", " ")[:250]
    return header("❌", "حصلت مشكلة") + f"\n`{detail}`\n\n{THIN}\n💡 {hint}"


def cancelled(hint: str = "ابعت /start لما تحب ترجع للقائمة 🏠") -> str:
    return header("🚫", "تم الإلغاء") + f"\n{hint}"
