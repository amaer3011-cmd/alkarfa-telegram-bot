"""
utils/pdf_watermark.py
========================
Best-effort watermark removal for PDF files, using PyMuPDF (`fitz`).

This module has **no built-in notion** of what a "watermark" looks like —
it only acts on patterns the user (or channel admin) explicitly configures
through the bot's menus. Two kinds of removal are supported, applied to
every page of the PDF:

1. Text removal — any text matching a plain string or a regular
   expression is covered with a redaction annotation and the redaction is
   then applied, which permanently deletes the underlying text/graphics
   in that rectangle (unlike just drawing a box over it, the content is
   actually gone from the page's content stream).
2. Link removal — any embedded hyperlink whose target URI contains a
   given substring is deleted from the page.

Pattern syntax (one pattern per line, blank lines ignored):
    plain text                 -> exact/substring text match on the page
    regex:<pattern>             -> Python regex match against each text span
    link:<substring>             -> removes links whose URI contains <substring>

Append ``=> <replacement>`` to REPLACE instead of remove:
    old text => نص جديد          -> the old text is deleted and the new one is
                                  written in its place (Arabic shaped properly)
    regex:t\\.me/\\w+ => t.me/new   -> same, for a regex (\\1 back-references work)
    link:t.me/old => https://t.me/new  -> the link target is swapped

A replacement that looks like a link (t.me/x, https://..., @name) is also made
clickable. Style lines (they are not patterns):
    font:amiri    color:#C0392B    size:14

This is a generic redaction tool — callers are responsible for only
running it against PDFs the user actually has the right to modify.
"""

import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from html import escape as _html_escape
from typing import Callable, List, Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 recommended import name
except ImportError:  # pragma: no cover - older PyMuPDF releases
    import fitz

logger = logging.getLogger(__name__)


FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")

# key -> (file in fonts/, Arabic label shown in the bot). All are SIL OFL fonts.
FONTS = {
    "amiri": ("Amiri-Bold.ttf", "أميري — نسخ كلاسيكي فخم"),
    "aref": ("ArefRuqaa-Bold.ttf", "عارف رقعة — خط الرقعة"),
    "rakkas": ("Rakkas-Regular.ttf", "رقّاص — ديواني مزخرف"),
    "lalezar": ("Lalezar-Regular.ttf", "لالزار — عريض للعناوين"),
    "tajawal": ("Tajawal-Bold.ttf", "تجوّل — عصري أنيق"),
    "almarai": ("Almarai-ExtraBold.ttf", "المراعي — عصري واضح"),
    "katibeh": ("Katibeh-Regular.ttf", "كاتبة — خط يدوي"),
    "mirza": ("Mirza-Bold.ttf", "ميرزا — نستعليق"),
    "scheherazade": ("ScheherazadeNew-Bold.ttf", "شهرزاد — نسخ مريح"),
}
DEFAULT_FONT = "amiri"

# color:<name> shortcuts (besides #RRGGBB)
COLORS = {
    "red": ("#C0392B", "🔴 أحمر"), "gold": ("#B7860B", "🟡 ذهبي"),
    "green": ("#1E8449", "🟢 أخضر"), "blue": ("#1F5FA8", "🔵 أزرق"),
    "purple": ("#7D3C98", "🟣 بنفسجي"), "black": ("#111111", "⚫ أسود"),
    "gray": ("#7F8C8D", "⚪ رمادي"),
}

_DIRECTIVE = re.compile(r"^\s*(font|color|size)\s*:\s*(.*?)\s*$", re.I)
_ARROW = re.compile(r"\s*=>\s*")


@dataclass
class WatermarkPattern:
    raw: str
    kind: str  # "text" | "regex" | "link"
    replacement: Optional[str] = None  # None -> just remove

    @classmethod
    def parse(cls, line: str) -> "WatermarkPattern":
        line = line.strip()
        lowered = line.lower()
        kind = "text"
        if lowered.startswith("regex:"):
            kind, line = "regex", line[len("regex:"):]
        elif lowered.startswith("link:"):
            kind, line = "link", line[len("link:"):]
        parts = _ARROW.split(line, maxsplit=1)
        raw = parts[0].strip()
        replacement = parts[1].strip() if len(parts) > 1 else ""
        return cls(raw=raw, kind=kind, replacement=replacement or None)


@dataclass
class WatermarkStyle:
    font: str = DEFAULT_FONT
    color: Optional[str] = None   # "#RRGGBB"; None -> same color as the removed text
    size: Optional[float] = None  # pt; None -> same size as the removed text


def parse_patterns(blob: "str | None") -> List[WatermarkPattern]:
    """Parse the raw, user-supplied multi-line pattern blob."""
    if not blob:
        return []
    patterns = []
    for line in blob.splitlines():
        if not line.strip() or _DIRECTIVE.match(line):
            continue
        pattern = WatermarkPattern.parse(line)
        if pattern.raw:
            patterns.append(pattern)
    return patterns


def _clean_color(value: str) -> Optional[str]:
    value = (value or "").strip()
    if value.lower() in COLORS:
        return COLORS[value.lower()][0]
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        return "#" + value.lstrip("#").upper()
    return None


def parse_style(blob: "str | None") -> WatermarkStyle:
    """Read the font:/color:/size: lines out of the blob (last one wins)."""
    style = WatermarkStyle()
    for line in (blob or "").splitlines():
        m = _DIRECTIVE.match(line)
        if not m:
            continue
        name, value = m.group(1).lower(), m.group(2)
        if name == "font" and value.lower() in FONTS:
            style.font = value.lower()
        elif name == "color":
            style.color = _clean_color(value)
        elif name == "size":
            try:
                size = float(value)
                style.size = size if 4 <= size <= 120 else None
            except ValueError:
                style.size = None
    return style


def set_directive(blob: "str | None", name: str, value: Optional[str]) -> str:
    """Return `blob` with the `name:` line replaced (or removed when value is None)."""
    kept = [ln for ln in (blob or "").splitlines()
            if not ((m := _DIRECTIVE.match(ln)) and m.group(1).lower() == name)]
    if value:
        kept.insert(0, f"{name}:{value}")
    return "\n".join(kept)


def merge_directives(old_blob: "str | None", new_blob: str) -> str:
    """Keep the saved font/color/size when the user re-sends only the patterns."""
    result = new_blob
    present = {m.group(1).lower() for ln in new_blob.splitlines() if (m := _DIRECTIVE.match(ln))}
    for ln in (old_blob or "").splitlines():
        m = _DIRECTIVE.match(ln)
        if m and m.group(1).lower() not in present:
            result = ln.strip() + "\n" + result
    return result


def describe_pattern(p: WatermarkPattern) -> str:
    kinds = {"text": "نص", "regex": "regex", "link": "رابط"}
    tail = f"  ⟵  {p.replacement}" if p.replacement else "  ⟵  (حذف)"
    return f"• {kinds[p.kind]}: {p.raw}{tail}"


# --------------------------------------------------------------------- #
# Text normalisation (so Arabic watermarks match however the PDF stored them)
# --------------------------------------------------------------------- #
# Arabic marks that must not affect matching: harakat, tatweel, etc.
_ARABIC_MARKS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_LETTER_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ک": "ك",
})
_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def _norm(text: str, lower: bool = False) -> str:
    """NFKC turns Arabic presentation forms (ﻗ ﻻ ...) back into plain letters."""
    out = unicodedata.normalize("NFKC", text)
    out = _ARABIC_MARKS.sub("", out).translate(_LETTER_MAP)
    return out.lower() if lower else out


@dataclass
class _Block:
    chars: list        # [(char, Rect)] — (None, None) marks a line break
    meta: list = None  # parallel to chars: (color_int, size_pt, is_horizontal) | None
    norm: str = ""     # normalised text, regex flavour
    norm_lower: str = ""
    index: list = None  # norm position -> position in `chars`

    def build(self) -> None:
        plain, lower, index = [], [], []
        for pos, (ch, _rect) in enumerate(self.chars):
            piece = " " if ch is None else _norm(ch)
            for sub in piece:
                plain.append(sub)
                lower.append(sub.lower()[:1] or sub)
                index.append(pos)
        self.norm = "".join(plain)
        self.norm_lower = "".join(lower)
        self.index = index


def _read_blocks(page) -> List[_Block]:
    blocks = []
    for b in page.get_text("rawdict").get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        chars: list = []
        meta: list = []
        for line_no, line in enumerate(b.get("lines", [])):
            if line_no and chars:
                chars.append((None, None))
                meta.append(None)
            d = line.get("dir", (1, 0))
            horizontal = abs(d[1]) < 0.1
            for span in line.get("spans", []):
                info = (span.get("color", 0), float(span.get("size", 11)), horizontal)
                for c in span.get("chars", []):
                    chars.append((c["c"], fitz.Rect(c["bbox"])))
                    meta.append(info)
        if chars:
            block = _Block(chars, meta)
            block.build()
            blocks.append(block)
    return blocks


def _find_all(haystack: str, needle: str):
    start = 0
    while needle:
        pos = haystack.find(needle, start)
        if pos < 0:
            return
        yield pos, pos + len(needle)
        start = pos + len(needle)


def _match_ranges(block: _Block, text_patterns, regexes) -> list:
    """[(start, end, replacement|None)] over block.norm for everything that must go."""
    ranges = []
    for pattern in text_patterns:
        needle = _norm(pattern.raw, lower=True).strip()
        if not needle:
            continue
        for a, b in _find_all(block.norm_lower, needle):
            ranges.append((a, b, pattern.replacement))
        # Some PDFs store Arabic/Hebrew in visual (reversed) order.
        if _RTL.search(needle):
            for a, b in _find_all(block.norm_lower, needle[::-1]):
                ranges.append((a, b, pattern.replacement))
    for compiled, replacement in regexes:
        for m in compiled.finditer(block.norm):
            if m.end() > m.start():
                text = None
                if replacement:
                    try:
                        text = m.expand(replacement)
                    except (re.error, IndexError):
                        text = replacement
                ranges.append((m.start(), m.end(), text))
    # longest-first at the same start, so overlapping rules don't fight
    ranges.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    return ranges


def _safe_point(rect, others) -> "fitz.Point":
    """
    A point inside the glyph's box that is not inside any *other* glyph's box.
    Redacting a tiny rectangle there deletes just this glyph — a big diagonal
    "PIRATE.COM" stamp must not take the body text underneath with it.
    """
    near = [o for o in others if o.intersects(rect)]
    best = None
    # coarse grid first; a finer one only when every coarse point hit other text
    for steps, margin in ((7, 0.15), (21, 0.05)):
        for i in range(steps):
            for j in range(steps):
                x = rect.x0 + rect.width * (margin + (1 - 2 * margin) * i / (steps - 1))
                y = rect.y0 + rect.height * (margin + (1 - 2 * margin) * j / (steps - 1))
                hits = 0
                for o in near:
                    if o.x0 <= x <= o.x1 and o.y0 <= y <= o.y1:
                        hits += 1
                if best is None or hits < best[0]:
                    best = (hits, x, y)
                    if hits == 0:
                        return fitz.Point(x, y)
    return fitz.Point(best[1], best[2])


@dataclass
class _Job:
    """A piece of text to write where a match used to be."""
    rect: "fitz.Rect"
    text: str
    color: int
    size: float
    lo: float = None  # free horizontal span around the match (nearest other text / page edge)
    hi: float = None


def _first_line_group(block: _Block, positions: list) -> list:
    """Positions of the first visual line of a (possibly multi-line) match."""
    group = [positions[0]]
    for prev, cur in zip(positions, positions[1:]):
        if any(block.chars[k][0] is None for k in range(prev + 1, cur)):
            break
        group.append(cur)
    return group


def _redact_text_matches(page, blocks, text_patterns, regexes) -> tuple:
    """Queue redactions for every match. Returns (matches, matched_rects, jobs, skipped)."""
    matches = 0
    skipped = 0
    doomed = set()  # (block number, char position)
    match_rects = []
    jobs: List[_Job] = []
    for b_no, block in enumerate(blocks):
        last_end = -1
        for start, end, replacement in _match_ranges(block, text_patterns, regexes):
            positions = sorted({block.index[k] for k in range(start, min(end, len(block.index)))})
            positions = [p for p in positions if block.chars[p][0] is not None]
            if not positions:
                continue
            overlapping = start < last_end
            last_end = max(last_end, end)
            union = fitz.Rect(block.chars[positions[0]][1])
            for p in positions:
                doomed.add((b_no, p))
                union |= block.chars[p][1]
            if overlapping:
                continue
            matches += 1
            match_rects.append(union)
            if replacement:
                group = _first_line_group(block, positions)
                rect = fitz.Rect(block.chars[group[0]][1])
                for p in group:
                    rect |= block.chars[p][1]
                color, size, horizontal = block.meta[group[0]]
                if horizontal:
                    jobs.append(_Job(rect, replacement, color, size))
                else:
                    skipped += 1  # rotated stamp: removed, but nothing is written on a slant
    if not doomed:
        return 0, [], [], 0

    others = [
        rect
        for b_no, block in enumerate(blocks)
        for pos, (ch, rect) in enumerate(block.chars)
        if ch is not None and (b_no, pos) not in doomed
    ]
    page_rect = page.rect
    for job in jobs:
        band = job.rect.height * 0.3
        lo, hi = page_rect.x0, page_rect.x1
        for o in others:
            if o.y1 <= job.rect.y0 + band or o.y0 >= job.rect.y1 - band:
                continue  # not on the same line
            if o.x1 <= job.rect.x0 + 0.5:
                lo = max(lo, o.x1)
            elif o.x0 >= job.rect.x1 - 0.5:
                hi = min(hi, o.x0)
        job.lo, job.hi = lo, hi
    for b_no, pos in doomed:
        ch, rect = blocks[b_no].chars[pos]
        if ch.isspace() or rect.is_empty:
            continue
        pt = _safe_point(rect, others)
        page.add_redact_annot(fitz.Rect(pt.x - 0.3, pt.y - 0.3, pt.x + 0.3, pt.y + 0.3), fill=False)
    return matches, match_rects, jobs, skipped


def _delete_links_under(page, rects, protected=frozenset()) -> int:
    """A removed "t.me/xyz" must not stay clickable: drop links lying on it."""
    removed = 0
    for link in page.get_links():
        if link.get("xref") in protected:
            continue
        lr = fitz.Rect(link["from"])
        area = lr.width * lr.height
        if area <= 0:
            continue
        for r in rects:
            inter = lr & r
            if not inter.is_empty and (inter.width * inter.height) / area >= 0.5:
                page.delete_link(link)
                removed += 1
                break
    return removed


# --------------------------------------------------------------------- #
# Writing the replacement text
# --------------------------------------------------------------------- #
_archive = None
_font_cache: dict = {}
_URL_RE = re.compile(
    r"^(?:https?://\S+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,}(?:/\S*)?|t\.me/\S+)$", re.I
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def _fonts_archive():
    global _archive
    if _archive is None:
        _archive = fitz.Archive(FONTS_DIR)
    return _archive


def _font_path(key: str) -> str:
    return os.path.join(FONTS_DIR, FONTS.get(key, FONTS[DEFAULT_FONT])[0])


def _to_uri(text: str) -> Optional[str]:
    """The link a replacement text should open, if it looks like one."""
    t = text.strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{4,}", t):
        return "https://t.me/" + t[1:]
    if _SCHEME_RE.match(t):
        return t
    if _URL_RE.match(t):
        return "https://" + t
    return None


def _color_hex(color_int: int, style: WatermarkStyle) -> str:
    if style.color:
        return style.color
    return "#%06X" % (int(color_int) & 0xFFFFFF)


def _fallback_write(page, box, text, size, color_hex, rtl) -> None:
    """Last resort when HTML layout fails: reshape + bidi by hand and draw."""
    import arabic_reshaper
    from bidi.algorithm import get_display

    visual = get_display(arabic_reshaper.reshape(text)) if rtl else text
    path = _font_path("amiri")
    font = fitz.Font(fontfile=path)
    width = font.text_length(visual, fontsize=size)
    x = box.x1 - width if rtl else box.x0
    rgb = tuple(int(color_hex[i:i + 2], 16) / 255 for i in (1, 3, 5))
    page.insert_font(fontname="wmfallback", fontfile=path)
    page.insert_text((x, (box.y0 + box.y1) / 2 + size * 0.3), visual,
                     fontsize=size, fontname="wmfallback", color=rgb)


def _write_replacement(page, job: _Job, style: WatermarkStyle) -> None:
    text = job.text
    size = max(5.0, min(style.size or job.size, 96.0))
    color_hex = _color_hex(job.color, style)
    rtl = bool(_RTL.search(text))
    font_key = style.font if style.font in FONTS else DEFAULT_FONT

    font = _font_cache.get(font_key)
    if font is None:
        font = _font_cache[font_key] = fitz.Font(fontfile=_font_path(font_key))
    margin = 4.0
    bounds = page.rect
    lo = max(job.lo if job.lo is not None else bounds.x0, bounds.x0 + margin)
    hi = min(job.hi if job.hi is not None else bounds.x1, bounds.x1 - margin)
    # Room we may use: next to the old text, up to the nearest other text on
    # the line (or the page margin). Too long -> shrink the font to fit.
    cx = (job.rect.x0 + job.rect.x1) / 2
    centered = abs(cx - (bounds.x0 + bounds.x1) / 2) < bounds.width * 0.06  # e.g. a centred footer
    if centered:
        available = max(2 * min(cx - lo, hi - cx), job.rect.width)
    elif rtl:
        available = max(min(job.rect.x1, hi) - lo, job.rect.width, 8.0)
    else:
        available = max(hi - max(job.rect.x0, lo), job.rect.width, 8.0)
    needed = font.text_length(text, fontsize=size) * 1.03
    if needed > available:
        size = max(4.0, size * available / needed)
        needed = font.text_length(text, fontsize=size) * 1.03
    if centered:
        x0, x1 = cx - needed / 2, cx + needed / 2
    elif rtl:
        x1 = min(job.rect.x1, hi)   # keep the old text's right edge
        x0 = x1 - needed
    else:
        x0 = max(job.rect.x0, lo)   # keep the old text's left edge
        x1 = x0 + needed
    height = size * 1.5
    cy = (job.rect.y0 + job.rect.y1) / 2
    box = fitz.Rect(x0, cy - height / 2, x1, cy + height / 2)

    css = "@font-face{font-family:%s;src:url(%s);}" % (font_key, FONTS[font_key][0])
    html = (
        '<div dir="%s" style="font-family:%s;font-size:%.2fpt;line-height:%.2fpt;color:%s;'
        'margin:0;padding:0;white-space:nowrap;text-align:%s">%s</div>'
        % ("rtl" if rtl else "ltr", font_key, size, size * 1.4, color_hex,
           "right" if rtl else "left", _html_escape(text))
    )
    try:
        spare, _scale = page.insert_htmlbox(box, html, css=css, archive=_fonts_archive())
        if spare < 0:
            raise RuntimeError("replacement text did not fit")
    except Exception:
        logger.exception("HTML text layout failed, using the fallback writer")
        _fallback_write(page, box, text, size, color_hex, rtl)

    uri = _to_uri(text)
    if uri:
        page.insert_link({"kind": fitz.LINK_URI, "from": box, "uri": uri})


def _apply_link_rules(page, link_patterns) -> tuple:
    """Remove / retarget links. Returns (count, xrefs of links we deliberately set)."""
    count = 0
    protected = set()
    for link in page.get_links():
        uri = link.get("uri") or ""
        rule = next((p for p in link_patterns if p.raw in uri), None)
        if rule is None:
            continue
        if rule.replacement:
            new_uri = _to_uri(rule.replacement) or rule.replacement
            link["uri"] = new_uri
            page.update_link(link)
            if link.get("xref"):
                protected.add(link["xref"])
        else:
            page.delete_link(link)
        count += 1
    return count, protected


def process_page(page, text_patterns, regexes, link_patterns, style: WatermarkStyle) -> tuple:
    """
    Apply every rule to one page. Returns (changes, rects) — `rects` are the
    page areas that were touched (used to highlight the preview).
    """
    changes = 0
    rects: list = []
    protected: set = set()

    if link_patterns:
        n, protected = _apply_link_rules(page, link_patterns)
        changes += n
        rects += [fitz.Rect(l["from"]) for l in page.get_links() if l.get("xref") in protected]

    if text_patterns or regexes:
        try:
            blocks = _read_blocks(page)
            count, match_rects, jobs, _skipped = _redact_text_matches(
                page, blocks, text_patterns, regexes
            )
        except Exception:
            logger.exception("Watermark text scan failed on page %s", page.number + 1)
            count, match_rects, jobs = 0, [], []
        if count:
            # Remove ONLY the matched text. The defaults would also blank
            # the pixels of any image under the box and delete vector
            # drawings (table borders, backgrounds) it touches.
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
            changes += count
            rects += match_rects
            _delete_links_under(page, match_rects, protected)
            for job in jobs:
                try:
                    _write_replacement(page, job, style)
                except Exception:
                    logger.exception("Could not write replacement text on page %s", page.number + 1)
    return changes, rects


def split_rules(patterns: List[WatermarkPattern]) -> tuple:
    """(text_patterns, [(compiled_regex, replacement)], link_patterns)."""
    text_patterns = [p for p in patterns if p.kind == "text"]
    regexes = []
    for p in patterns:
        if p.kind == "regex":
            try:
                regexes.append((re.compile(p.raw), p.replacement))
            except re.error:
                logger.warning("Skipping invalid watermark regex: %r", p.raw)
    link_patterns = [p for p in patterns if p.kind == "link"]
    return text_patterns, regexes, link_patterns


def remove_watermarks(
    pdf_path: str,
    patterns: List[WatermarkPattern],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    style: Optional[WatermarkStyle] = None,
) -> int:
    """
    Apply every pattern to every page of the PDF at `pdf_path`, in place.

    Returns the number of matches removed or replaced (0 means the file was
    left untouched — including when no patterns were supplied).

    How text is removed: the page text is read glyph by glyph, matched on a
    normalised copy (Arabic presentation forms, diacritics, hamza variants and
    visual-order Arabic all still match), and ONLY the matched glyphs are
    redacted — each with a tiny rectangle placed where no other glyph is, so
    rotated / oversized stamps never damage the text around them. Links lying
    on a removed text are deleted too. When a pattern carries a replacement,
    the new text is written where the old one was, in `style.font`.

    `progress_callback(pages_done, total_pages)`, if given, is called
    after each page is processed. Runs on whatever thread called this
    function (it's meant to be run via a thread-pool executor).
    """
    style = style or WatermarkStyle()
    text_patterns, regexes, link_patterns = split_rules(patterns)
    if not text_patterns and not regexes and not link_patterns:
        return 0

    removed = 0
    doc = fitz.open(pdf_path)
    try:
        total_pages = doc.page_count
        for page_index, page in enumerate(doc):
            changes, _rects = process_page(page, text_patterns, regexes, link_patterns, style)
            removed += changes

            if progress_callback:
                try:
                    progress_callback(page_index + 1, total_pages)
                except Exception:
                    # Progress reporting must never abort the actual redaction.
                    logger.exception("Watermark progress_callback raised")

        if removed:
            # Save to a temp file first, then swap in place — writing over
            # a file that's still open under fitz isn't safe.
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
            return removed
    finally:
        if not doc.is_closed:
            doc.close()

    return removed
