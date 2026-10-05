from __future__ import annotations

import io
import logging
import re
import unicodedata
from typing import Iterable

import fitz

logger = logging.getLogger(__name__)

# Value-cell rectangles measured against the standard DIAN 500 page in the
# supplied DIM sample (622 x 890 points). Scale them to the actual page size.
# Labels remain visible; each value cell is physically redacted.
_REFERENCE_PAGE_SIZE = (622.0, 890.0)
_SENSITIVE_FIELD_RECTS: dict[int, tuple[float, float, float, float]] = {
    46: (24.5, 338.2, 394.5, 348.2),
    47: (395.8, 338.2, 497.0, 348.2),
    49: (24.5, 356.2, 351.5, 366.2),
    50: (352.0, 356.2, 497.0, 366.2),
    78: (24.5, 451.0, 102.5, 460.5),
    80: (24.5, 495.2, 102.5, 505.2),
    82: (24.5, 519.3, 102.5, 529.3),
    84: (24.5, 543.2, 102.5, 553.2),
}
_FIELD_LABELS: dict[int, re.Pattern[str]] = {
    46: re.compile(r"\b46\s+nombre\s+exportador\s+o\s+proveedor\s+en\s+el\s+exterior\b"),
    47: re.compile(r"\b47\s+ciudad\b"),
    49: re.compile(r"\b49\s+direccion\s+exportador\s+o\s+proveedor\s+en\s+el\s+exterior\b"),
    50: re.compile(r"\b50\s+e\s*mail\b"),
    78: re.compile(r"\b78\s+valor\s+fob\s+usd\b"),
    80: re.compile(r"\b80\s+valor\s+seguros\s+usd\b"),
    82: re.compile(r"\b82\s+sumatoria\s+de\s+fletes\s+seguros\s+y\s+otros\s+gastos\s+usd\b"),
    84: re.compile(r"\b84\s+valor\s+aduana\s+usd\b"),
}


def _normalized_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _scaled_rect(page: fitz.Page, coordinates: tuple[float, float, float, float]) -> fitz.Rect:
    width_scale = page.rect.width / _REFERENCE_PAGE_SIZE[0]
    height_scale = page.rect.height / _REFERENCE_PAGE_SIZE[1]
    x0, y0, x1, y1 = coordinates
    return fitz.Rect(x0 * width_scale, y0 * height_scale, x1 * width_scale, y1 * height_scale)


def _add_redaction(page: fitz.Page, rectangle: fitz.Rect) -> None:
    page.add_redact_annot(rectangle, fill=(0, 0, 0), cross_out=False)


def redact_pdf(pdf_bytes: bytes, terms: Iterable[str] = ()) -> bytes:
    """Physically remove sensitive DIAN 500 field values and purge metadata.

    Fixed field masking is mandatory. Optional caller-provided terms are also
    physically redacted, but generic environment terms (for example "NIT") are
    intentionally not used because they can black out labels instead of data.
    """
    custom_terms = {
        term.strip() for term in terms
        if isinstance(term, str) and term.strip()
    }
    try:
        logger.info("PDF redaction started (%s optional terms)", len(custom_terms))
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            found_fields: set[int] = set()
            target_rectangles: dict[int, list[fitz.Rect]] = {}
            redacted_pages: set[int] = set()

            for page_number, page in enumerate(document, start=1):
                page_text = _normalized_text(page.get_text("text"))
                matched_fields = {
                    field_number
                    for field_number, pattern in _FIELD_LABELS.items()
                    if pattern.search(page_text)
                }
                if matched_fields:
                    found_fields.update(matched_fields)
                    rectangles = [
                        _scaled_rect(page, _SENSITIVE_FIELD_RECTS[field_number])
                        for field_number in sorted(matched_fields)
                    ]
                    target_rectangles[page_number - 1] = rectangles
                    for rectangle in rectangles:
                        _add_redaction(page, rectangle)
                    redacted_pages.add(page_number - 1)
                    logger.info(
                        "PDF redaction: page %s contains target DIAN fields %s",
                        page_number,
                        sorted(matched_fields),
                    )

                # Additional values are only redacted when explicitly supplied
                # by the request; do not apply broad defaults such as "nit".
                for term in custom_terms:
                    for rectangle in page.search_for(term):
                        if any(rectangle.intersects(target) for target in target_rectangles.get(page_number - 1, [])):
                            continue
                        _add_redaction(page, rectangle)
                        redacted_pages.add(page_number - 1)

            missing_fields = sorted(set(_SENSITIVE_FIELD_RECTS) - found_fields)
            if missing_fields:
                raise ValueError(
                    "No se pudo verificar la plantilla DIM: faltan las casillas "
                    + ", ".join(map(str, missing_fields))
                )

            for page_number, page in enumerate(document):
                if page_number not in redacted_pages:
                    continue
                # Apply redaction to vector text and overwrite image pixels in
                # the target cells. This prevents recovering the original text
                # by selecting text or lifting a black overlay.
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

            if callable(getattr(document, "scrub", None)):
                document.scrub()
            document.set_metadata({})
            if document.xref_xml_metadata():
                document.del_xml_metadata()

            output = io.BytesIO()
            document.save(output, garbage=4, deflate=True, clean=True)
            sanitized = output.getvalue()

        # Reopen the exported bytes and verify the field cells, metadata, and
        # XMP packet are empty before returning anything to the upload handler.
        with fitz.open(stream=sanitized, filetype="pdf") as verified:
            for page_number, rectangles in target_rectangles.items():
                page = verified[page_number]
                for rectangle in rectangles:
                    if page.get_textbox(rectangle).strip():
                        raise RuntimeError("La verificación detectó texto residual en una casilla censurada")
            metadata = verified.metadata or {}
            if any(value for key, value in metadata.items() if key not in {"format", "encryption"}):
                raise RuntimeError("La verificación detectó metadatos residuales en el PDF")
            if verified.xref_xml_metadata():
                raise RuntimeError("La verificación detectó metadatos XMP residuales en el PDF")

        logger.info("PDF redaction verified (%s bytes)", len(sanitized))
        return sanitized
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("PDF redaction failed:"):
            raise
        logger.exception("PDF redaction failed")
        raise RuntimeError(f"PDF redaction failed: {exc}") from exc
    except Exception as exc:
        logger.exception("Unexpected PDF redaction failure")
        raise RuntimeError("PDF redaction failed unexpectedly") from exc
