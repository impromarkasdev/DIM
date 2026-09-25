from __future__ import annotations

import io
from typing import Iterable

import fitz


def redact_pdf(pdf_bytes: bytes, terms: Iterable[str]) -> bytes:
    """Destroy matched text and image pixels, then purge recoverable PDF remnants."""
    cleaned_terms = {term.strip() for term in terms if isinstance(term, str) and term.strip()}
    if not cleaned_terms:
        raise ValueError("At least one sensitive term must be supplied for redaction")
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        redactions = 0
        for page in document:
            for term in cleaned_terms:
                for rectangle in page.search_for(term):
                    page.add_redact_annot(rectangle, fill=(0, 0, 0), cross_out=False)
                    redactions += 1
            # This removes, rather than merely obscures, matching text and any
            # image content intersecting the redaction rectangle.
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)
        if redactions == 0:
            raise ValueError("None of the requested sensitive terms was found in the PDF")
        document.set_metadata({})
        if document.xref_xml_metadata():
            document.del_xml_metadata()
        output = io.BytesIO()
        document.save(output, garbage=4, deflate=True, clean=True)
        document.close()
        return output.getvalue()
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise RuntimeError("PDF redaction failed") from exc
