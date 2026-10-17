"""
utils/pdf_tools.py
====================
The PDF studio's engine: merge, split, extract pages, reorder, rotate and
images -> PDF. Everything here is blocking (PyMuPDF / Pillow) so callers run it
through an executor, and every result is re-opened and checked by
`verify_pdf` before it is handed back, so a broken file never leaves the bot.
"""

import io
import logging
import os
import re
from typing import Callable, List, Optional, Sequence, Tuple

from PIL import Image, ImageOps

from utils.integrity import IntegrityError, verify_pdf

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz

logger = logging.getLogger(__name__)

MAX_SPLIT_PARTS = 50

A4 = (595.0, 842.0)
MAX_IMAGE_SIDE = 3508          # A4 at 300 dpi
IMAGE_JPEG_QUALITY = 90

LAYOUT_FIT = "fit"             # page has the image's own proportions
LAYOUT_A4 = "a4"               # image centred on an A4 page
LAYOUT_LABELS = {LAYOUT_FIT: "مقاس الصورة", LAYOUT_A4: "A4"}


class ToolError(ValueError):
    """A user-facing (Arabic) problem: bad page numbers, unreadable file..."""


# --------------------------------------------------------------------- #
# Page-number parsing
# --------------------------------------------------------------------- #
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_LAST_WORDS = {"end", "last", "اخر", "آخر", "الاخر", "الآخر", "الاخيرة", "الأخيرة", "n"}
_ALL_WORDS = {"all", "كل", "الكل"}


def _normalize_spec(spec: str) -> str:
    spec = (spec or "").translate(_DIGITS).lower()
    spec = spec.replace("،", ",").replace("؛", ",").replace(";", ",").replace("\n", ",")
    spec = re.sub(r"[–—ـ‑−]", "-", spec)
    spec = re.sub(r"\b(to|الى|إلى)\b", "-", spec)
    return re.sub(r"\s*-\s*", "-", spec)


def _page_token(tok: str, count: int) -> int:
    tok = tok.strip()
    if tok in _LAST_WORDS:
        return count
    if not tok.isdigit():
        raise ToolError(f"مش فاهم «{tok}» — اكتب أرقام صفحات زي: 1-3,7,10")
    n = int(tok)
    if n < 1 or n > count:
        raise ToolError(f"الصفحة {n} مش موجودة — الملف فيه {count} صفحة بس")
    return n


def parse_pages(spec: str, count: int, allow_desc: bool = False, dedupe: bool = True) -> List[int]:
    """
    "1-3,7,10-end" -> [0, 1, 2, 6, 9, ...] (0-based, in the order written).
    `allow_desc` lets "5-1" mean 5,4,3,2,1 (used when reordering).
    """
    text = _normalize_spec(spec).strip()
    if not text:
        raise ToolError("ما كتبتش أي صفحات")
    if text in _ALL_WORDS:
        return list(range(count))
    out: List[int] = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if part in _ALL_WORDS:
            out.extend(range(count))
            continue
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2 or not bits[0] or not bits[1]:
                raise ToolError(f"النطاق «{part}» مش مظبوط — اكتبه زي 3-8")
            a, b = _page_token(bits[0], count), _page_token(bits[1], count)
            if a > b:
                if not allow_desc:
                    raise ToolError(f"النطاق «{part}» معكوس — اكتب الأصغر الأول")
                out.extend(range(a - 1, b - 2, -1))
            else:
                out.extend(range(a - 1, b))
        else:
            out.append(_page_token(part, count) - 1)
    if dedupe:
        seen = set()
        out = [p for p in out if not (p in seen or seen.add(p))]
    if not out:
        raise ToolError("ما كتبتش أي صفحات")
    return out


def parse_ranges(spec: str, count: int) -> List[List[int]]:
    """"1-5, 6-10, 12" -> one list of pages per comma-separated part (each becomes a file)."""
    text = _normalize_spec(spec)
    groups = [g for g in re.split(r"[,]+", text) if g.strip()]
    if not groups:
        raise ToolError("ما كتبتش أي نطاق")
    return [parse_pages(g, count) for g in groups]


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def _open(path: str, label: str = "") -> "fitz.Document":
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ToolError(f"الملف {label}مش بيتفتح كـ PDF ({exc})")
    if not doc.is_pdf:
        doc.close()
        raise ToolError(f"الملف {label}مش PDF")
    if doc.needs_pass:
        doc.close()
        raise ToolError(f"الملف {label}محمي بكلمة مرور")
    if doc.page_count < 1:
        doc.close()
        raise ToolError(f"الملف {label}مفيهوش صفحات")
    return doc


def page_count(path: str) -> int:
    doc = _open(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _save(doc: "fitz.Document", dst: str, expected_pages: int) -> str:
    tmp = dst + ".tmp"
    try:
        doc.save(tmp, garbage=3, deflate=True, clean=True)
    finally:
        doc.close()
    try:
        verify_pdf(tmp, expected_pages)
        os.replace(tmp, dst)
    except IntegrityError as exc:
        raise ToolError(f"الملف الناتج طلع مش سليم ({exc})")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return dst


# --------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------- #
def merge_pdfs(
    paths: Sequence[str], names: Sequence[str], dst: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Append every PDF in order. Returns the total page count."""
    out = fitz.open()
    total = 0
    try:
        for i, path in enumerate(paths):
            src = _open(path, f"«{names[i]}» " if i < len(names) else "")
            try:
                out.insert_pdf(src)
                total += src.page_count
            finally:
                src.close()
            if progress:
                progress(i + 1, len(paths))
        _save(out, dst, total)
    except BaseException:
        out.close()
        raise
    return total


def _select(src: str, pages: Sequence[int], dst: str) -> int:
    doc = _open(src)
    try:
        doc.select(list(pages))
    except Exception:
        doc.close()
        raise
    return _save_len(doc, dst, len(pages))


def _save_len(doc: "fitz.Document", dst: str, n: int) -> int:
    _save(doc, dst, n)
    return n


def extract_pages(src: str, spec: str, dst: str) -> Tuple[int, int]:
    """Keep only the listed pages (in the written order). Returns (kept, original)."""
    count = page_count(src)
    pages = parse_pages(spec, count)
    return _select(src, pages, dst), count


def reorder_pages(src: str, spec: str, dst: str) -> Tuple[int, int, List[int]]:
    """
    New page order. Pages the user didn't mention are appended at the end (so no
    page is ever lost by accident). Returns (pages, original, appended_pages_1based).
    """
    count = page_count(src)
    order = parse_pages(spec, count, allow_desc=True)
    missing = [p for p in range(count) if p not in set(order)]
    return _select(src, order + missing, dst), count, [p + 1 for p in missing]


def reverse_pages(src: str, dst: str) -> int:
    count = page_count(src)
    return _select(src, list(range(count - 1, -1, -1)), dst)


def rotate_pages(src: str, angle: int, spec: Optional[str], dst: str) -> Tuple[int, int]:
    """Rotate the listed pages (all if `spec` is None). Returns (rotated, total)."""
    if angle % 90:
        raise ToolError("زاوية التدوير لازم تكون 90 أو 180 أو 270")
    doc = _open(src)
    try:
        count = doc.page_count
        pages = list(range(count)) if spec is None else parse_pages(spec, count)
        for p in pages:
            page = doc[p]
            page.set_rotation((page.rotation + angle) % 360)
    except BaseException:
        doc.close()
        raise
    _save(doc, dst, count)
    return len(pages), count


def split_ranges_of(count: int, mode: str, value: Optional[str]) -> List[List[int]]:
    """The page groups for a split. mode: every | parts | ranges | each."""
    if mode == "each":
        groups = [[i] for i in range(count)]
    elif mode == "every":
        n = _positive_int(value, "عدد الصفحات")
        groups = [list(range(i, min(i + n, count))) for i in range(0, count, n)]
    elif mode == "parts":
        k = _positive_int(value, "عدد الأجزاء")
        if k > count:
            raise ToolError(f"الملف فيه {count} صفحة بس، مينفعش يتقسم {k} أجزاء")
        base, extra = divmod(count, k)
        groups, start = [], 0
        for i in range(k):
            size = base + (1 if i < extra else 0)
            groups.append(list(range(start, start + size)))
            start += size
    elif mode == "ranges":
        groups = parse_ranges(value or "", count)
    else:
        raise ToolError("طريقة التقسيم مش معروفة")
    if len(groups) > MAX_SPLIT_PARTS:
        raise ToolError(
            f"ده هيطلّع {len(groups)} ملف وده كتير (الحد {MAX_SPLIT_PARTS}). اختار تقسيم أكبر."
        )
    if len(groups) < 2 and mode != "ranges":
        raise ToolError("التقسيم ده هيطلّع ملف واحد بس — اختار رقم أصغر")
    return groups


def _positive_int(value: Optional[str], what: str) -> int:
    text = (value or "").translate(_DIGITS).strip()
    if not text.isdigit() or int(text) < 1:
        raise ToolError(f"{what} لازم يكون رقم أكبر من صفر")
    return int(text)


def split_pdf(
    src: str, groups: Sequence[Sequence[int]], dst_for: Callable[[int, Sequence[int]], str],
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[str]:
    """Write one PDF per page group. `dst_for(i, pages)` gives each output path."""
    outputs: List[str] = []
    try:
        for i, pages in enumerate(groups):
            dst = dst_for(i, pages)
            _select(src, pages, dst)
            outputs.append(dst)
            if progress:
                progress(i + 1, len(groups))
    except BaseException:
        for path in outputs:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return outputs


# --------------------------------------------------------------------- #
# Images -> PDF
# --------------------------------------------------------------------- #
def _image_bytes(path: str) -> Tuple[bytes, int, int]:
    """(JPEG/PNG bytes ready to embed, width, height). Honours EXIF rotation, flattens alpha."""
    try:
        im = Image.open(path)
        im.load()
    except Exception as exc:
        raise ToolError(f"صورة مش بتتفتح ({exc})")
    orig_format = im.format
    oriented = ImageOps.exif_transpose(im)
    rotated = oriented is not im
    im = oriented

    # Untouched JPEGs that are already a sane size are embedded as they are (no quality loss).
    if (
        orig_format == "JPEG" and not rotated and max(im.size) <= MAX_IMAGE_SIDE
        and im.mode in ("RGB", "L")
    ):
        with open(path, "rb") as fh:
            return fh.read(), im.width, im.height

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[-1])
        im = flat
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > MAX_IMAGE_SIDE:
        k = MAX_IMAGE_SIDE / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), im.width, im.height


def images_to_pdf(
    paths: Sequence[str], dst: str, layout: str = LAYOUT_FIT,
    progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """One image per page, in order. Returns the page count."""
    if not paths:
        raise ToolError("مفيش صور")
    doc = fitz.open()
    try:
        for i, path in enumerate(paths):
            data, w, h = _image_bytes(path)
            if layout == LAYOUT_A4:
                pw, ph = A4 if h >= w else (A4[1], A4[0])  # landscape images get a landscape page
                scale = min(pw / w, ph / h)
                iw, ih = w * scale, h * scale
                page = doc.new_page(width=pw, height=ph)
                x0, y0 = (pw - iw) / 2, (ph - ih) / 2
                rect = fitz.Rect(x0, y0, x0 + iw, y0 + ih)
            else:
                pw = A4[0]
                ph = pw * h / w
                page = doc.new_page(width=pw, height=ph)
                rect = page.rect
            page.insert_image(rect, stream=data)
            if progress:
                progress(i + 1, len(paths))
        n = doc.page_count
    except BaseException:
        doc.close()
        raise
    _save(doc, dst, n)
    return n
