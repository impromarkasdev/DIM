from __future__ import annotations

import io
import re
from collections import OrderedDict
from typing import Iterable

import fitz


FORM_NUMBER = re.compile(r"\b0?3\d{12,14}-\d\b")


def _is_blank(page: fitz.Page) -> bool:
    """Treat pages with neither meaningful text nor meaningful rendered marks as blank."""
    if page.get_text("text").strip():
        return False
    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2), colorspace=fitz.csGRAY, alpha=False)
    # White is 255. A low count of darker pixels means an empty scanned page.
    return sum(pixel < 245 for pixel in pixmap.samples) < 40


def split_declarations(pdf_bytes: bytes) -> list[tuple[str, bytes]]:
    try:
        source = fitz.open(stream=pdf_bytes, filetype="pdf")
    except fitz.FileDataError as exc:
        raise ValueError("Uploaded file is not a readable PDF") from exc
    groups: OrderedDict[str, list[int]] = OrderedDict()
    current: str | None = None
    for page_number, page in enumerate(source):
        if _is_blank(page):
            continue
        match = FORM_NUMBER.search(page.get_text("text"))
        if match:
            current = match.group(0)
            groups.setdefault(current, [])
        if current is not None:
            groups[current].append(page_number)
    if not groups:
        raise ValueError("No declaration form number was found in the PDF")
    result: list[tuple[str, bytes]] = []
    for form_number, pages in groups.items():
        document = fitz.open()
        for page_number in pages:
            document.insert_pdf(source, from_page=page_number, to_page=page_number)
        document.set_metadata({})
        output = io.BytesIO()
        document.save(output, garbage=4, deflate=True, clean=True)
        document.close()
        result.append((f"{form_number}.pdf", output.getvalue()))
    source.close()
    return result
