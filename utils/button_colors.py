"""
utils/button_colors.py
========================
Gives every inline button a colour (Telegram's button styles), without
touching each keyboard by hand:

* 🟢 green  — confirm / add / enable / unban
* 🔴 red    — delete / ban / cancel / stop
* 🔵 blue   — everything else (menus, back, navigation…)

Works by wrapping `InlineKeyboardButton.__init__`, so it also covers the
buttons built dynamically in the handlers. Needs Kurigram (the maintained
Pyrogram fork) — the old `pyrogram` package ignores the style.
Import this module once, before any keyboard is built (main.py does).
"""

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton

_GREEN = ("✅", "➕", "♻️", "🔊", "📥", "💾", "▶️", "فك ", "تأكيد", "حفظ", "ابعت لـ", "تفعيل")
_RED = ("🗑", "🚫", "❌", "⏹", "🧹", "🔇", "حذف", "إلغاء", "الغاء", "إنهاء", "شيل")


def pick_style(text: str) -> ButtonStyle:
    text = text or ""
    # "فك الحظر" / "✅ فك حظر" must be green even though they contain "حظر"
    if any(k in text for k in _GREEN):
        return ButtonStyle.SUCCESS
    if any(k in text for k in _RED):
        return ButtonStyle.DANGER
    return ButtonStyle.PRIMARY


def install() -> None:
    if getattr(InlineKeyboardButton, "_colored", False):
        return
    original = InlineKeyboardButton.__init__

    def colored_init(self, text, *args, **kwargs):
        original(self, text, *args, **kwargs)
        if self.style in (None, ButtonStyle.DEFAULT):
            self.style = pick_style(text)

    InlineKeyboardButton.__init__ = colored_init
    InlineKeyboardButton._colored = True
