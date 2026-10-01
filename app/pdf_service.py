from __future__ import annotations

import io
import logging
from typing import Iterable

import fitz

logger = logging.getLogger(__name__)


def redact_pdf(pdf_bytes: bytes, terms: Iterable[str]) -> bytes:
    """Destroy matched text and image pixels, then purge recoverable PDF remnants."""
    cleaned_terms = {term.strip() for term in terms if isinstance(term, str) and term.strip()}
    if not cleaned_terms:
        raise ValueError("At least one sensitive term must be supplied for redaction")
    try:
        logger.info("PDF redaction started (%s sensitive terms)", len(cleaned_terms))
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            redactions = 0
            for page_number, page in enumerate(document, start=1):
                for term in cleaned_terms:
                    for rectangle in page.search_for(term):
                        page.add_redact_annot(rectangle, fill=(0, 0, 0), cross_out=False)
                        redactions += 1
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)
                logger.info("PDF redaction page %s complete", page_number)
            if redactions == 0:
                raise ValueError("None of the requested sensitive terms was found in the PDF")
            document.set_metadata({})
            if document.xref_xml_metadata():
                document.del_xml_metadata()
            output = io.BytesIO()
            document.save(output, garbage=4, deflate=True, clean=True)
            logger.info("PDF redaction complete (%s redactions)", redactions)
            return output.getvalue()
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise RuntimeError("PDF redaction failed") from exc
