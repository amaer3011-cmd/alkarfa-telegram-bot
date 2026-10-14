"""
utils/integrity.py
====================
Checks that make sure nothing broken ever leaves the bot.

* verify_download  -> the downloaded file is complete (size == Telegram's size)
* verify_pdf       -> the PDF opens, has the expected page count and renders
* verify_upload    -> what Telegram stored has the same size as what we sent

Video checks live in utils/video_edit.py (verify_video) because they need ffmpeg.
"""

import logging
import os
from typing import Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz

logger = logging.getLogger(__name__)


class IntegrityError(RuntimeError):
    """A file failed a soundness check."""


def verify_download(path: Optional[str], expected_size: int = 0) -> None:
    if not path or not os.path.exists(path):
        raise IntegrityError("الملف مش موجود بعد التحميل")
    size = os.path.getsize(path)
    if size == 0:
        raise IntegrityError("الملف اتحمّل فاضي")
    if expected_size and size != expected_size:
        raise IntegrityError(f"التحميل ناقص ({size} من {expected_size} بايت)")


def inspect_pdf(path: str):
    """(page_count, needs_password). Blocking. Raises if the PDF can't be opened."""
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return 0, True
        return doc.page_count, False
    finally:
        doc.close()


def verify_pdf(path: str, expected_pages: Optional[int] = None) -> int:
    """
    Open the PDF, check the page count and render the first and last page.
    Blocking (call through an executor). Returns the page count.
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise IntegrityError(f"الـ PDF مش بيتفتح ({exc})")
    try:
        if not doc.is_pdf:
            raise IntegrityError("الملف مش PDF صالح")
        if doc.needs_pass:
            raise IntegrityError("الـ PDF بقى محمي بكلمة مرور")
        count = doc.page_count
        if count < 1:
            raise IntegrityError("الـ PDF مفيهوش صفحات")
        if expected_pages is not None and count != expected_pages:
            raise IntegrityError(f"عدد الصفحات غلط ({count} بدل {expected_pages})")
        for index in {0, count - 1}:
            try:
                doc.load_page(index).get_pixmap(matrix=fitz.Matrix(0.1, 0.1), alpha=False)
            except Exception as exc:
                raise IntegrityError(f"الصفحة {index + 1} مش بتتعرض ({exc})")
        return count
    finally:
        doc.close()


def verify_upload(sent, local_size: int) -> None:
    """`sent` is the pyrogram Message returned by send_*; compare sizes."""
    if sent is None:
        raise IntegrityError("تيليجرام ما رجّعش رسالة بعد الرفع")
    media = (
        getattr(sent, "video", None) or getattr(sent, "audio", None)
        or getattr(sent, "document", None)
    )
    if media is None:
        raise IntegrityError("الرسالة اللي اتبعتت مفيهاش ملف")
    remote = getattr(media, "file_size", None)
    if remote and local_size and remote != local_size:
        raise IntegrityError(f"حجم الملف على تيليجرام ({remote}) مختلف عن الأصلي ({local_size})")
