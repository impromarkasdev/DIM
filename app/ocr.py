from __future__ import annotations

import io
import logging
import os
import time
from typing import Sequence
from urllib.parse import urlparse

import fitz
import requests
from PIL import Image

logger = logging.getLogger(__name__)
API_VERSION = "2024-11-30"
MAX_POLL_SECONDS = 150


def _campo4_region(page: fitz.Page) -> fitz.Rect:
    bounds = page.rect
    return fitz.Rect(
        bounds.x0 + bounds.width * 0.54,
        bounds.y0 + bounds.height * 0.06,
        bounds.x1,
        bounds.y0 + bounds.height * 0.25,
    )


def _azure_read_pdf(pdf_bytes: bytes, page_indexes: Sequence[int], endpoint: str, key: str) -> dict[int, str]:
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
        raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT debe ser una URL HTTPS válida")

    analyze_url = (
        f"{endpoint.rstrip('/')}/documentintelligence/documentModels/"
        f"prebuilt-read:analyze?api-version={API_VERSION}"
    )
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"}
    session = requests.Session()
    try:
        response = session.post(analyze_url, data=pdf_bytes, headers=headers, timeout=(5, 40))
        if response.status_code in {429, 500, 502, 503, 504}:
            try:
                delay = min(max(float(response.headers.get("Retry-After", "1")), 0), 5.0)
            except (TypeError, ValueError):
                delay = 1.0
            time.sleep(delay)
            response = session.post(analyze_url, data=pdf_bytes, headers=headers, timeout=(5, 40))
        if response.status_code != 202:
            logger.error("Azure OCR submit failed (HTTP %s) for %s pages", response.status_code, len(page_indexes))
            raise RuntimeError(f"Azure Document Intelligence rechazó el OCR (HTTP {response.status_code})")

        operation_url = response.headers.get("Operation-Location", "")
        operation = urlparse(operation_url)
        if operation.scheme != "https" or operation.netloc.casefold() != parsed_endpoint.netloc.casefold():
            raise RuntimeError("Azure Document Intelligence devolvió una ubicación de operación inválida")

        deadline = time.monotonic() + MAX_POLL_SECONDS
        while time.monotonic() < deadline:
            time.sleep(1)
            result = session.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=(5, 12))
            if result.status_code in {429, 500, 502, 503, 504}:
                continue
            result.raise_for_status()
            payload = result.json()
            status = str(payload.get("status", "")).casefold()
            if status == "succeeded":
                pages = payload.get("analyzeResult", {}).get("pages", [])
                recognized: dict[int, str] = {}
                for page in pages:
                    try:
                        crop_page_number = int(page.get("pageNumber", 0))
                        original_index = page_indexes[crop_page_number - 1]
                    except (ValueError, TypeError, IndexError):
                        continue
                    lines = page.get("lines", [])
                    recognized[original_index] = "\n".join(
                        str(line.get("content", "")) for line in lines if isinstance(line, dict)
                    )
                logger.info("Azure OCR completed for %s Campo 4 crops", len(recognized))
                return recognized
            if status == "failed":
                logger.error("Azure OCR analysis failed for %s pages", len(page_indexes))
                raise RuntimeError("Azure Document Intelligence no pudo leer las casillas 4")
        raise TimeoutError("Azure Document Intelligence excedió el tiempo de espera del OCR")
    except requests.RequestException as exc:
        logger.warning("Azure OCR transport failure (%s)", type(exc).__name__)
        raise RuntimeError("No fue posible conectar con Azure Document Intelligence") from exc
    finally:
        session.close()


def _tesseract_page(page: fitz.Page, page_number: int) -> str:
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError(
            "OCR no configurado: añade las credenciales de Azure Document Intelligence en Vercel"
        ) from exc
    tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=_campo4_region(page), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    try:
        return pytesseract.image_to_string(image, config="--oem 1 --psm 6")
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "OCR no disponible: configura Azure Document Intelligence o instala el binario Tesseract local"
        ) from exc


def recognize_campo4_pages(document: fitz.Document, page_indexes: Sequence[int]) -> dict[int, str]:
    """OCR just Campo 4 for all needed pages in one Azure operation when configured."""
    indexes = list(dict.fromkeys(int(index) for index in page_indexes))
    if not indexes:
        return {}
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip().rstrip("/")
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()
    if endpoint or key:
        if not endpoint or not key:
            raise RuntimeError("Configura AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT y AZURE_DOCUMENT_INTELLIGENCE_KEY")
        # Azure receives a temporary PDF containing only the Campo 4 crop of
        # pages that lacked usable text, not the full confidential DIM.
        with fitz.open() as crops:
            for index in indexes:
                page = document.load_page(index)
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(2.5, 2.5), clip=_campo4_region(page), alpha=False
                )
                crop_page = crops.new_page(width=pixmap.width, height=pixmap.height)
                crop_page.insert_image(crop_page.rect, stream=pixmap.tobytes("png"))
            cropped_pdf = crops.tobytes(garbage=4, deflate=True, clean=True)
        return _azure_read_pdf(cropped_pdf, indexes, endpoint, key)

    logger.warning("Azure Document Intelligence is not configured; trying local Tesseract fallback")
    return {index: _tesseract_page(document.load_page(index), index + 1) for index in indexes}
