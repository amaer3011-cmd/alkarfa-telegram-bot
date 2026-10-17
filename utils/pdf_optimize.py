"""
utils/pdf_optimize.py
=======================
"Quality + smallest size" for PDFs.

What it really does (honest version): it never touches text or vector graphics
(they stay razor sharp at any zoom), and it re-encodes only the *images* inside
the file:

* an image stored at a far higher resolution than it is ever shown at is scaled
  down to the level's target DPI (e.g. a 600-dpi scan shown on a phone/A4 page);
* photos / scans are re-encoded as optimised progressive JPEG;
* flat graphics with few colours (diagrams, screenshots) are re-encoded as
  optimised PNG so they don't get JPEG artefacts;
* an image is replaced only if the new version is clearly smaller;
* finally the whole file is rewritten with object-stream + deflate compression
  and unused objects are dropped.

Safety net (same spirit as the rest of the bot): the result is re-opened and
checked (page count, pages render); if it's broken or not smaller than the
original, the caller keeps the original file.

All functions here are blocking: call them through an executor.
"""

import io
import logging
import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from PIL import Image

from utils.integrity import IntegrityError, verify_pdf

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Level:
    key: str
    button: str       # text on the choose-level button
    label: str        # short name shown in messages
    max_dpi: int      # images shown at more than this are scaled down to it
    jpeg_quality: int


LEVELS: Dict[str, Level] = {
    "high": Level("high", "✨ جودة عالية جدًا (موصى به)", "جودة عالية جدًا", 220, 82),
    "balanced": Level("balanced", "⚖️ متوازن (حجم أصغر)", "متوازن", 170, 72),
    "small": Level("small", "🗜 أقصى ضغط (أصغر حجم)", "أقصى ضغط", 120, 58),
}
DEFAULT_LEVEL = "high"

# Images below this many pixels are not worth the work (icons, bullets, rules).
_MIN_PIXELS = 40_000
# An image is replaced only if it gets at least this much smaller.
_MIN_GAIN = 0.90


@dataclass
class OptimizeResult:
    before: int = 0
    after: int = 0
    images_total: int = 0
    images_changed: int = 0
    improved: bool = False   # False => the original file was already as small as it gets

    @property
    def saved_percent(self) -> float:
        return 100.0 * (self.before - self.after) / self.before if self.before else 0.0


def _encode_image(pix: "fitz.Pixmap", scale: float, quality: int) -> Optional[bytes]:
    """Re-encode one image; returns the new bytes (JPEG or PNG) or None if unsupported."""
    cs = pix.colorspace
    if cs is None:
        return None
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)  # drop alpha (images with a real soft-mask are skipped earlier)
    if cs.n not in (1, 3):  # CMYK / DeviceN / Lab -> RGB
        pix = fitz.Pixmap(fitz.csRGB, pix)
    mode = "L" if pix.n == 1 else "RGB"
    im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)

    if scale < 1.0:
        new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        im = im.resize(new_size, Image.LANCZOS)

    # Flat graphics (few colours): PNG keeps edges perfectly clean and is tiny.
    probe = im if max(im.size) <= 1200 else im.resize(
        (max(1, im.width * 1200 // max(im.size)), max(1, im.height * 1200 // max(im.size))),
        Image.NEAREST,
    )
    few_colors = probe.getcolors(maxcolors=64) is not None
    buf = io.BytesIO()
    if few_colors:
        im.save(buf, format="PNG", optimize=True)
    else:
        # 4:4:4 for grayscale/text-like scans keeps thin strokes crisp; 4:2:0 for colour photos.
        im.save(
            buf, format="JPEG", quality=quality, optimize=True, progressive=True,
            subsampling=0 if mode == "L" else 2,
        )
    return buf.getvalue()


def _image_scale(page: "fitz.Page", xref: int, width_px: int, height_px: int, max_dpi: int) -> float:
    """How much this image can be scaled down (<= 1.0) without going under `max_dpi`."""
    shown_w = 0.0
    try:
        for rect in page.get_image_rects(xref):
            rw, rh = rect.width, rect.height
            if rw <= 1 or rh <= 1:
                continue
            # A page/image rotated by 90 degrees swaps the shown width and height.
            ratio = width_px / max(1, height_px)
            straight = abs(ratio - rw / rh)
            swapped = abs(ratio - rh / rw)
            shown_w = max(shown_w, rw if straight <= swapped else rh)
    except Exception:
        shown_w = 0.0
    if shown_w <= 1:
        shown_w = page.rect.width  # e.g. inside a form: the page width is an upper bound
    eff_dpi = width_px / (shown_w / 72.0)
    return min(1.0, max_dpi / eff_dpi) if eff_dpi > 0 else 1.0


def optimize_pdf(
    src: str,
    dst: str,
    level_key: str = DEFAULT_LEVEL,
    progress: Optional[Callable[[int, int], None]] = None,
) -> OptimizeResult:
    """
    Write an optimised copy of `src` to `dst`. If the result isn't clearly
    smaller (or isn't a sound PDF) `dst` is NOT created and `improved` is False,
    so the caller simply keeps `src`.
    """
    level = LEVELS.get(level_key) or LEVELS[DEFAULT_LEVEL]
    result = OptimizeResult(before=os.path.getsize(src))

    doc = fitz.open(src)
    try:
        if doc.needs_pass:
            raise IntegrityError("الـ PDF محمي بكلمة مرور")
        pages = doc.page_count
        if pages < 1:
            raise IntegrityError("الـ PDF مفيهوش صفحات")

        done: set = set()
        for index in range(pages):
            page = doc[index]
            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            for img in images:
                xref, smask, width, height, bpc = img[0], img[1], img[2], img[3], img[4]
                if xref in done:
                    continue
                done.add(xref)
                result.images_total += 1
                # Skip: tiny images, 1-bit scans/stencils (already tiny, JPEG would bloat them),
                # and images that carry a soft mask (re-encoding would drop the transparency).
                if width * height < _MIN_PIXELS or bpc == 1 or smask:
                    continue
                try:
                    old_len = len(doc.xref_stream_raw(xref))
                    pix = fitz.Pixmap(doc, xref)
                    scale = _image_scale(page, xref, width, height, level.max_dpi)
                    data = _encode_image(pix, scale, level.jpeg_quality)
                    pix = None
                    if data is not None and len(data) < old_len * _MIN_GAIN:
                        page.replace_image(xref, stream=data)
                        result.images_changed += 1
                except Exception as exc:  # one odd image must never fail the whole file
                    logger.debug("image %s skipped: %s", xref, exc)
            if progress:
                progress(index + 1, pages)

        tmp = dst + ".tmp"
        try:
            doc.save(
                tmp, garbage=4, clean=True, deflate=True, deflate_images=True,
                deflate_fonts=True, use_objstms=1,
            )
        finally:
            doc.close()
            doc = None

        try:
            result.after = os.path.getsize(tmp)
            if result.after >= result.before * 0.98:
                return result  # nothing worth keeping
            verify_pdf(tmp, pages)  # raises IntegrityError if the output isn't sound
            os.replace(tmp, dst)
            result.improved = True
            return result
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    finally:
        if doc is not None:
            doc.close()
