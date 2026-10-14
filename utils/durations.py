"""
utils/durations.py
====================
Parsing and printing of the time spans the owner types in the admin panel
("30m", "2h", "3d", "1w", "ساعتين", "٣ ايام", ...).
"""

import re
from typing import Optional

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# word -> seconds (matched on the start of the unit word the user typed)
_UNITS = (
    (("m", "min", "د", "دقيق", "دقاي"), 60),
    (("h", "hr", "hour", "س", "ساع"), 3600),
    (("d", "day", "ي", "يوم", "ايام", "أيام"), 86400),
    (("w", "week", "ا", "أ", "اسبوع", "أسبوع", "اسابيع", "أسابيع", "ش"), 7 * 86400),
    (("mo", "month", "شهر", "شهور", "أشهر", "اشهر"), 30 * 86400),
)
_FOREVER = {"0", "دايم", "دائم", "للابد", "للأبد", "نهائي", "permanent", "forever", "inf"}


def parse_duration(text: str) -> Optional[int]:
    """Seconds for the text, 0 for "forever", None if it can't be understood."""
    t = (text or "").strip().lower().translate(_DIGITS)
    if not t:
        return None
    if t in _FOREVER:
        return 0
    # a few words with no number
    words = {"ساعة": 3600, "ساعه": 3600, "ساعتين": 7200, "يوم": 86400, "يومين": 2 * 86400,
             "اسبوع": 7 * 86400, "أسبوع": 7 * 86400, "اسبوعين": 14 * 86400, "شهر": 30 * 86400,
             "دقيقة": 60, "دقيقتين": 120}
    if t in words:
        return words[t]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([^\d\s.]+)", t)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2)
    best = None
    # longest prefix wins so "mo"/"month" beat "m"
    for names, secs in _UNITS:
        for name in names:
            if unit.startswith(name) and (best is None or len(name) > best[0]):
                best = (len(name), secs)
    if best is None or num <= 0:
        return None
    return int(num * best[1])


def human(seconds: int) -> str:
    """'ساعتين', '3 أيام', '45 دقيقة' ... (0 => دايمًا)."""
    if not seconds:
        return "دايمًا"
    seconds = int(seconds)
    for size, one, two, many in (
        (30 * 86400, "شهر", "شهرين", "شهور"),
        (7 * 86400, "أسبوع", "أسبوعين", "أسابيع"),
        (86400, "يوم", "يومين", "أيام"),
        (3600, "ساعة", "ساعتين", "ساعات"),
        (60, "دقيقة", "دقيقتين", "دقايق"),
    ):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            if n == 1:
                return one
            if n == 2:
                return two
            return f"{n} {many}"
    if seconds >= 60:
        return f"{round(seconds / 60)} دقيقة"
    return f"{seconds} ثانية"
