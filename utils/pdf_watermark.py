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
    plain text            -> exact/substring text match on the page
    regex:<pattern>        -> Python regex match against each text span
    link:<substring>        -> removes links whose URI contains <substring>

This is a generic redaction tool — callers are responsible for only
running it against PDFs the user actually has the right to modify.
"""

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 recommended import name
except ImportError:  # pragma: no cover - older PyMuPDF releases
    import fitz

logger = logging.getLogger(__name__)


@dataclass
class WatermarkPattern:
    raw: str
    kind: str  # "text" | "regex" | "link"

    @classmethod
    def parse(cls, line: str) -> "WatermarkPattern":
        line = line.strip()
        lowered = line.lower()
        if lowered.startswith("regex:"):
            return cls(raw=line[len("regex:"):].strip(), kind="regex")
        if lowered.startswith("link:"):
            return cls(raw=line[len("link:"):].strip(), kind="link")
        return cls(raw=line, kind="text")


def parse_patterns(blob: "str | None") -> List[WatermarkPattern]:
    """Parse the raw, user-supplied multi-line pattern blob."""
    if not blob:
        return []
    patterns = []
    for line in blob.splitlines():
        if not line.strip():
            continue
        pattern = WatermarkPattern.parse(line)
        if pattern.raw:
            patterns.append(pattern)
    return patterns


def remove_watermarks(
    pdf_path: str,
    patterns: List[WatermarkPattern],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """
    Apply every pattern to every page of the PDF at `pdf_path`, in place.

    Returns the number of matches removed (0 means the file was left
    untouched — including when no patterns were supplied).

    `progress_callback(pages_done, total_pages)`, if given, is called
    after each page is processed — useful for reporting progress on
    large books (hundreds of pages) where this can take a while.
    Runs on whatever thread called this function (it's meant to be run
    via a thread-pool executor), so the callback must be safe to call
    from a non-event-loop thread.
    """
    text_patterns = [p for p in patterns if p.kind in ("text", "regex")]
    link_patterns = [p for p in patterns if p.kind == "link"]
    if not text_patterns and not link_patterns:
        return 0

    removed = 0
    doc = fitz.open(pdf_path)
    try:
        total_pages = doc.page_count
        for page_index, page in enumerate(doc):
            page_has_redactions = False
            text_dict = None  # parsed once per page, shared by all regex patterns

            for pattern in text_patterns:
                if pattern.kind == "text":
                    for rect in page.search_for(pattern.raw):
                        page.add_redact_annot(rect, fill=False)
                        page_has_redactions = True
                        removed += 1
                else:  # regex
                    try:
                        compiled = re.compile(pattern.raw)
                    except re.error:
                        logger.warning("Skipping invalid watermark regex: %r", pattern.raw)
                        continue
                    if text_dict is None:
                        text_dict = page.get_text("dict")
                    for block in text_dict.get("blocks", []):
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                if compiled.search(span.get("text", "")):
                                    page.add_redact_annot(
                                        fitz.Rect(span["bbox"]), fill=False
                                    )
                                    page_has_redactions = True
                                    removed += 1

            if page_has_redactions:
                # Remove ONLY the matched text. The defaults would also blank
                # the pixels of any image under the box and delete vector
                # drawings (table borders, backgrounds) it touches.
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                )

            if link_patterns:
                for link in page.get_links():
                    uri = link.get("uri") or ""
                    if any(pattern.raw in uri for pattern in link_patterns):
                        page.delete_link(link)
                        removed += 1

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
