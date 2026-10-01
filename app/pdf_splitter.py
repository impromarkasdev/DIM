from __future__ import annotations

import io
import logging
import os
import re
from collections import OrderedDict
import fitz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# DIMs normally use a long numeric declaration (e.g. 032026000884560-6),
# but older documents can expose an alphanumeric declaration code.
# Numeric DIM forms (for example ``032026001248077-6``) and historic
# alphanumeric declarations (for example ``A367-ZF305D``).  The expression
# deliberately preserves the hyphen because it is the public filename key.
FORM_NUMBER = re.compile(r"(?<!\d)\d{15}\s*-\s*\d(?!\d)")


def _is_blank(page: fitz.Page) -> bool:
    """Treat pages with neither meaningful text nor meaningful rendered marks as blank."""
    text = re.sub(r"\s+", "", page.get_text("text"))
    if len(text) >= 3:
        return False
    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2), colorspace=fitz.csGRAY, alpha=False)
    # White is 255. A low count of darker pixels means an empty scanned page.
    return sum(pixel < 245 for pixel in pixmap.samples) < 40


def _find_form_number(text: str) -> str | None:
    """Return a canonical 15-digit DIAN Campo 4 number despite OCR spacing."""
    compact_text = re.sub(r"\s+", "", text)
    match = FORM_NUMBER.search(compact_text)
    return match.group(0) if match else None


def _ocr_page_text(page: fitz.Page, page_number: int) -> str:
    """OCR a rendered page when its selectable text has no Campo 4 match."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("OCR requerido pero faltan pytesseract/Pillow; instálelos junto con el binario Tesseract") from exc
    try:
        tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        pixmap = page.get_pixmap(dpi=250, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        logger.info("PDF split: OCR started on page %s", page_number)
        text = pytesseract.image_to_string(image, config="--oem 1 --psm 6")
        logger.info("PDF split: OCR completed on page %s (%s characters)", page_number, len(text))
        return text
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("OCR requerido pero el ejecutable Tesseract no está instalado/configurado") from exc
    except Exception as exc:
        logger.exception("PDF split: OCR failed on page %s", page_number)
        raise RuntimeError(f"OCR failed on page {page_number}") from exc


def split_declarations(pdf_bytes: bytes) -> list[tuple[str, bytes]]:
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as source:
            page_texts = [page.get_text("text") for page in source]
            # Keep source page indexes: fitz.Page objects are tied to the
            # document lifetime and cannot safely outlive this context.
            groups: OrderedDict[str, list[int]] = OrderedDict()
            current: str | None = None
            for page_number, (page, text) in enumerate(zip(source, page_texts)):
                if _is_blank(page):
                    logger.info("PDF split: blank page %s skipped", page_number + 1)
                    continue
                # OCR engines may insert whitespace inside the form number or
                # split it across lines. Compact only for matching and retain
                # the canonical matched token (with its hyphen) as filename.
                form_number = _find_form_number(text)
                if form_number is None:
                    logger.info("PDF split: text extraction did not find Campo 4 on page %s; trying OCR", page_number + 1)
                    form_number = _find_form_number(_ocr_page_text(page, page_number + 1))
                if form_number:
                    current = form_number
                    groups.setdefault(current, [])
                    logger.info("PDF split: declaration %s matched on page %s", current, page_number + 1)
                else:
                    logger.warning("PDF split: Campo 4 regex did not match on page %s after text extraction and OCR", page_number + 1)
                if current is not None:
                    groups[current].append(page_number)
            if not groups:
                logger.warning("PDF split: readable text exists but no declaration regex matched")
                raise ValueError("No se encontró el Número de Formulario de 15 dígitos-guion-dígito en el PDF, ni con OCR")
            result: list[tuple[str, bytes]] = []
            for form_number, pages in groups.items():
                with fitz.open() as document:
                    for page_number in pages:
                        document.insert_pdf(source, from_page=page_number, to_page=page_number)
                    document.set_metadata({})
                    result.append((f"{form_number}.pdf", document.tobytes(garbage=4, deflate=True, clean=True)))
            return result
    except fitz.FileDataError as exc:
        raise ValueError("Uploaded file is not a readable PDF") from exc
    except ValueError:
        raise


def split_pages(pdf_bytes: bytes, original_name: str) -> list[tuple[str, bytes]]:
    """Copy nonblank pages with their existing PDF objects; avoid expensive re-rendering."""
    try:
        source = fitz.open(stream=pdf_bytes, filetype="pdf")
    except fitz.FileDataError as exc:
        raise ValueError("The OneDrive item is not a readable PDF") from exc
    result: list[tuple[str, bytes]] = []
    with source:
        stem = original_name.rsplit(".", 1)[0]
        for page_number, page in enumerate(source, start=1):
            if _is_blank(page):
                continue
            with fitz.open() as document:
                document.insert_pdf(source, from_page=page_number - 1, to_page=page_number - 1)
                output = io.BytesIO()
                document.save(output, garbage=0, deflate=False)
                result.append((f"{stem}_pagina_{page_number:03d}.pdf", output.getvalue()))
    if not result: raise ValueError("The PDF contains only blank pages")
    return result
