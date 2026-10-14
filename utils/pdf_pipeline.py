"""
utils/pdf_pipeline.py
=======================
One place that applies every PDF edit (watermark removal, then the
first-page image) so the private chat flow and the channel auto-formatter
behave identically.

Each step is best-effort: a failure is logged, reported back in the
result, and never prevents the (possibly unedited) file from being sent.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Callable, Optional

from utils.integrity import inspect_pdf, verify_pdf
from utils.pdf_cover import MODE_INSERT, MODE_REPLACE, add_cover_page, normalize_mode
from utils.pdf_watermark import parse_patterns, remove_watermarks

logger = logging.getLogger(__name__)


@dataclass
class PdfResult:
    watermarks_removed: int = 0
    cover_added: bool = False
    warnings: list = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


async def process_pdf(
    pdf_path: str,
    *,
    watermark_patterns: Optional[str] = None,
    cover_path: Optional[str] = None,
    cover_mode: str = MODE_INSERT,
    watermark_progress: Optional[Callable[[int, int], None]] = None,
    on_stage: Optional[Callable[[str], "asyncio.Future"]] = None,
) -> PdfResult:
    """
    Edit the PDF in place. Safety net: the original is backed up first, the
    result is re-opened and checked (page count, pages render), and if
    anything is wrong the original file is put back untouched.
    """
    result = PdfResult()
    loop = asyncio.get_running_loop()

    patterns = parse_patterns(watermark_patterns)
    use_cover = bool(cover_path and os.path.exists(cover_path))
    if not patterns and not use_cover:
        return result

    try:
        pages, needs_pass = await loop.run_in_executor(None, inspect_pdf, pdf_path)
    except Exception as exc:
        result.warnings.append(f"الـ PDF الأصلي مش بيتفتح فاتبعت زي ما هو ({exc})")
        return result
    if needs_pass:
        result.warnings.append("الـ PDF محمي بكلمة مرور فاتبعت من غير تعديل")
        return result

    backup = pdf_path + ".bak"
    await loop.run_in_executor(None, shutil.copyfile, pdf_path, backup)
    try:
        if patterns:
            if on_stage:
                await on_stage("watermark")
            try:
                result.watermarks_removed = await loop.run_in_executor(
                    None, remove_watermarks, pdf_path, patterns, watermark_progress
                )
            except Exception as exc:
                logger.exception("Watermark removal failed")
                result.warnings.append(f"تعذّرت إزالة العلامة المائية ({exc})")

        if use_cover:
            if on_stage:
                await on_stage("cover")
            try:
                await loop.run_in_executor(None, add_cover_page, pdf_path, cover_path, cover_mode)
                result.cover_added = True
            except Exception as exc:
                logger.exception("Adding PDF cover failed")
                result.warnings.append(f"تعذّر وضع الصورة في أول صفحة ({exc})")

        if result.watermarks_removed or result.cover_added:
            expected = pages
            if result.cover_added and not (normalize_mode(cover_mode) == MODE_REPLACE and pages >= 1):
                expected = pages + 1
            try:
                await loop.run_in_executor(None, verify_pdf, pdf_path, expected)
            except Exception as exc:
                logger.error("PDF failed verification, restoring the original: %s", exc)
                await loop.run_in_executor(None, shutil.copyfile, backup, pdf_path)
                result.warnings.append(
                    "التعديل على الـ PDF طلّع ملف مش سليم فاتبعت الأصلي من غير تعديل"
                )
                result.watermarks_removed = 0
                result.cover_added = False
    finally:
        if os.path.exists(backup):
            try:
                os.remove(backup)
            except OSError:
                logger.warning("Could not remove %s", backup)

    return result
