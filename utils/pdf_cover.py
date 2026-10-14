"""
utils/pdf_cover.py
====================
Put an image on the first page of a PDF (PyMuPDF).

Two modes:
    insert   -> the image becomes a NEW page 1 (the original pages shift
                down by one; nothing is lost).
    replace  -> the image REPLACES the original first page.

The new page always takes the size of the PDF's current first page, and
the image is scaled to fit it while keeping its aspect ratio (centered on
a white page), so the document keeps a uniform page size.

Also contains `render_first_page_thumbnail`, used to generate a Telegram
thumbnail from page 1 when the user has no thumbnail configured.
"""

import logging
import os
import shutil
import tempfile
from typing import Optional

from PIL import Image, ImageOps

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz

logger = logging.getLogger(__name__)

MODE_INSERT = "insert"
MODE_REPLACE = "replace"
MODE_LABELS = {
    MODE_INSERT: "إضافة كصفحة جديدة قبل الأولى",
    MODE_REPLACE: "استبدال الصفحة الأولى",
}
_A4 = (595.0, 842.0)


def normalize_mode(mode: Optional[str]) -> str:
    return mode if mode in MODE_LABELS else MODE_INSERT


def next_mode(mode: Optional[str]) -> str:
    return MODE_REPLACE if normalize_mode(mode) == MODE_INSERT else MODE_INSERT


_IMAGE_EXTS = (".jpg", ".png")


def find_image(stem: str) -> Optional[str]:
    """Return the existing image saved under `stem` (.jpg or .png), if any."""
    for ext in _IMAGE_EXTS:
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def _remove_other_variants(stem: str, keep: str) -> None:
    for ext in _IMAGE_EXTS:
        path = stem + ext
        if path != keep and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def store_image_copy(src_path: str, stem: str) -> str:
    """Copy an already-prepared image to `stem` keeping its own extension."""
    final = stem + os.path.splitext(src_path)[1].lower()
    if os.path.abspath(src_path) != os.path.abspath(final):
        shutil.copyfile(src_path, final)
    _remove_other_variants(stem, final)
    return final


def prepare_cover_image(source_path: str, dest_stem: str) -> str:
    """
    Store the user's image at `dest_stem` WITHOUT losing quality, and return
    the final path (the extension depends on the format). Blocking.

    * JPEG  -> the original bytes are kept untouched (zero re-compression)
              and PyMuPDF embeds them in the PDF as-is.
    * PNG   -> the original bytes are kept (lossless, transparency kept).
    * other -> (WebP, BMP, TIFF, GIF...) converted LOSSLESSLY to PNG.
    * The only re-encode of a JPEG is when its EXIF orientation says it must
      be rotated (or it is CMYK): then it is saved at quality 100, 4:4:4.

    There is no resizing: full resolution is preserved.
    """
    Image.MAX_IMAGE_PIXELS = None  # large originals are fine here
    with Image.open(source_path) as img:
        fmt = (img.format or "").upper()
        orientation = img.getexif().get(0x0112, 1) or 1
        rotated = orientation != 1

        if fmt == "JPEG" and not rotated and img.mode in ("RGB", "L"):
            final = dest_stem + ".jpg"
            shutil.copyfile(source_path, final)
        elif fmt == "PNG" and not rotated:
            final = dest_stem + ".png"
            shutil.copyfile(source_path, final)
        elif fmt == "JPEG":
            fixed = ImageOps.exif_transpose(img).convert("RGB")
            final = dest_stem + ".jpg"
            fixed.save(
                final, "JPEG", quality=100, subsampling=0,
                icc_profile=img.info.get("icc_profile"),
            )
        else:
            fixed = ImageOps.exif_transpose(img)
            if fixed.mode not in ("RGB", "RGBA", "L", "LA"):
                has_alpha = "transparency" in fixed.info or fixed.mode in ("PA", "RGBa", "La")
                fixed = fixed.convert("RGBA" if has_alpha else "RGB")
            final = dest_stem + ".png"
            fixed.save(final, "PNG", compress_level=6)

    _remove_other_variants(dest_stem, final)
    return final


def add_cover_page(pdf_path: str, image_path: str, mode: str = MODE_INSERT) -> None:
    """
    Add `image_path` as the first page of the PDF at `pdf_path`, in place.
    Blocking — call through a thread executor. Raises on failure (the
    original file is left untouched in that case).
    """
    mode = normalize_mode(mode)
    doc = fitz.open(pdf_path)
    try:
        if doc.needs_pass:
            raise ValueError("الملف محمي بكلمة مرور")

        if doc.page_count:
            rect = doc[0].rect
            width, height = rect.width, rect.height
        else:
            width, height = _A4
        if width < 50 or height < 50:
            width, height = _A4

        if mode == MODE_REPLACE and doc.page_count > 1:
            doc.delete_page(0)
            page = doc.new_page(pno=0, width=width, height=height)
        elif mode == MODE_REPLACE and doc.page_count == 1:
            doc.new_page(pno=0, width=width, height=height)
            doc.delete_page(1)
            page = doc[0]
        else:
            page = doc.new_page(pno=0, width=width, height=height)

        page.insert_image(page.rect, filename=image_path, keep_proportion=True)

        # Fast path: for "insert" append the new page to the end of the file
        # instead of rewriting the whole PDF (near-instant even for books of
        # hundreds of MB). Falls back to a full save if the file can't take
        # an incremental update (e.g. it was repaired on open).
        if mode == MODE_INSERT and doc.can_save_incrementally():
            try:
                doc.saveIncr()
                return
            except Exception:
                logger.info("Incremental save failed, falling back to full save")

        directory = os.path.dirname(pdf_path) or "."
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=directory)
        os.close(fd)
        try:
            doc.save(tmp_path, garbage=3, deflate=True)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        doc.close()
        os.replace(tmp_path, pdf_path)
    finally:
        if not doc.is_closed:
            doc.close()


def render_first_page_thumbnail(pdf_path: str, dest_path: str, max_side: int = 320) -> bool:
    """Render page 1 as a small JPEG. Returns False if it can't be done."""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return False
    try:
        if doc.needs_pass or doc.page_count == 0:
            return False
        page = doc[0]
        zoom = max_side / max(page.rect.width, page.rect.height, 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(dest_path)
        return os.path.exists(dest_path)
    except Exception:
        logger.exception("Could not render PDF thumbnail")
        return False
    finally:
        doc.close()
