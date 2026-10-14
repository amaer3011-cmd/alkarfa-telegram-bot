"""
utils/wm_preview.py
=====================
Previews for the PDF watermark tool — nothing here writes the user's file.

* build_preview()       -> one PNG: the page BEFORE (matches boxed in red) next
                           to the page AFTER the rules were applied.
* render_font_gallery() -> one PNG that shows a sample text in every font the
                           bot offers, so the user can pick by eye.

Everything is drawn with PyMuPDF (HarfBuzz shaping), so Arabic letters join
correctly whatever fonts / Pillow build the server has.
"""

import logging
from dataclasses import dataclass
from html import escape
from typing import List, Optional

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from utils.pdf_watermark import (
    COLORS,
    FONTS,
    FONTS_DIR,
    WatermarkPattern,
    WatermarkStyle,
    _font_path,
    process_page,
    split_rules,
)

logger = logging.getLogger(__name__)

UI_FONT = "tajawal"
_BG = (0.97, 0.95, 0.91)
_INK = "#2b2b2b"


@dataclass
class PreviewInfo:
    page_no: int          # 1-based page that is shown
    changes: int          # total matches found in the scanned pages
    pages_hit: int        # how many scanned pages have at least one match
    pages_scanned: int
    total_pages: int


def _css() -> str:
    return "".join(
        "@font-face{font-family:%s;src:url(%s);}" % (key, fname) for key, (fname, _l) in FONTS.items()
    )


def _text(page, rect, html: str) -> None:
    page.insert_htmlbox(rect, html, css=_css(), archive=fitz.Archive(FONTS_DIR))


def _label(txt: str, size: float = 13, color: str = _INK, align: str = "center", font: str = UI_FONT) -> str:
    return (
        '<div dir="rtl" style="font-family:%s;font-size:%spt;color:%s;text-align:%s;margin:0">%s</div>'
        % (font, size, color, align, escape(txt))
    )


def render_font_gallery(sample: str, out_png: str, color_hex: Optional[str] = None) -> str:
    """A card per font, each showing `sample` in that font. Returns out_png."""
    color_hex = color_hex or COLORS["red"][0]
    sample = (sample or "قناة الكرفة").strip()[:60]
    rows = list(FONTS.items())
    width, row_h, top, pad = 520.0, 62.0, 56.0, 10.0
    height = top + len(rows) * (row_h + 8) + pad

    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=_BG)
    _text(page, fitz.Rect(pad, 12, width - pad, 46), _label("🎨 اختر الخط الأنسب لعلامتك", 17))

    for i, (key, (_f, name)) in enumerate(rows):
        y = top + i * (row_h + 8)
        card = fitz.Rect(pad, y, width - pad, y + row_h)
        page.draw_rect(card, color=(0.85, 0.80, 0.70), fill=(1, 1, 1), width=0.8, radius=0.12)
        _text(page, fitz.Rect(card.x0 + 8, y + row_h / 2 - 9, card.x0 + 150, y + row_h / 2 + 9),
              _label(f"{i + 1}. {name}", 9.5, "#7a6a50", "left"))
        # measure the sample so the box hugs it (right-aligned like Arabic text)
        size = 25.0
        need = fitz.Font(fontfile=_font_path(key)).text_length(sample, fontsize=size) * 1.04
        avail = card.width - 170
        if need > avail:
            size, need = size * avail / need, avail
        x1 = card.x1 - 14
        _text(page, fitz.Rect(x1 - need, y + 3, x1, y + row_h - 3),
              '<div dir="rtl" style="font-family:%s;font-size:%.1fpt;line-height:%.1fpt;color:%s;'
              'white-space:nowrap;margin:0;padding-top:%.1fpt">%s</div>'
              % (key, size, size * 1.4, color_hex, max(0.0, (row_h - 6 - size * 1.4) / 2), escape(sample)))

    page.get_pixmap(dpi=130).save(out_png)
    doc.close()
    return out_png


def _png(page, dpi: int) -> bytes:
    return page.get_pixmap(dpi=dpi).tobytes("png")


def build_preview(
    pdf_path: str,
    patterns: List[WatermarkPattern],
    style: WatermarkStyle,
    out_png: str,
    max_scan_pages: int = 40,
) -> PreviewInfo:
    """
    Run the rules on a COPY of the PDF (in memory) and draw before / after for
    the first page where something changed. If nothing matches, page 1 is shown
    twice and `changes` is 0 so the caller can say so.
    """
    text_patterns, regexes, link_patterns = split_rules(patterns)
    before = fitz.open(pdf_path)
    after = fitz.open(pdf_path)
    try:
        total = after.page_count
        scan = min(total, max_scan_pages)
        shown, shown_rects = None, []
        changes = pages_hit = 0
        for idx in range(scan):
            n, rects = process_page(after[idx], text_patterns, regexes, link_patterns, style)
            if n:
                changes += n
                pages_hit += 1
                if shown is None:
                    shown, shown_rects = idx, rects
        if shown is None:
            shown = 0

        # red boxes on the "before" page, where the old text/links are
        bpage = before[shown]
        for r in shown_rects:
            bpage.draw_rect(fitz.Rect(r) + (-2, -1.5, 2, 1.5), color=(0.88, 0.1, 0.1), width=1.3)

        dpi = 100
        img_before = _png(bpage, dpi)
        img_after = _png(after[shown], dpi)
        pw, ph = bpage.rect.width, bpage.rect.height
    finally:
        before.close()
        after.close()

    scale = 330.0 / pw  # each page is drawn 330pt wide on the card
    cw, ch = 330.0, ph * scale
    gap, head = 14.0, 44.0
    out = fitz.open()
    sheet = out.new_page(width=cw * 2 + gap * 3, height=head + ch + gap * 2)
    sheet.draw_rect(sheet.rect, color=None, fill=_BG)
    right = fitz.Rect(gap, head, gap + cw, head + ch)                 # RTL: "before" on the right
    left = fitz.Rect(gap * 2 + cw, head, gap * 2 + cw * 2, head + ch)
    # RTL reading order: before (right) -> after (left)
    right, left = left, right
    sheet.insert_image(right, stream=img_before)
    sheet.insert_image(left, stream=img_after)
    sheet.draw_rect(right, color=(0.7, 0.65, 0.55), width=0.8)
    sheet.draw_rect(left, color=(0.7, 0.65, 0.55), width=0.8)
    _text(sheet, fitz.Rect(right.x0, 8, right.x1, head - 2), _label("قبل", 15, "#a02020"))
    _text(sheet, fitz.Rect(left.x0, 8, left.x1, head - 2), _label("بعد", 15, "#1e7a3c"))
    sheet.get_pixmap(dpi=120).save(out_png)
    out.close()

    return PreviewInfo(
        page_no=shown + 1,
        changes=changes,
        pages_hit=pages_hit,
        pages_scanned=scan,
        total_pages=total,
    )
