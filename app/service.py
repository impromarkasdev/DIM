from __future__ import annotations

import base64
import io
from datetime import datetime
from typing import Any

import pandas as pd

from .config import Settings
from .graph import GraphClient
from .pdf_service import redact_pdf


def _cell_text(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _find_documents(workbook: bytes, references: list[str], settings: Settings) -> dict[str, tuple[str, str] | None]:
    try:
        sheets = pd.read_excel(io.BytesIO(workbook), sheet_name=None, header=None, dtype=object, engine="openpyxl")
    except Exception as exc:
        raise RuntimeError("Could not read the Excel workbook") from exc
    current_year = datetime.now().year
    ordered_sheets = [str(year) for year in range(current_year, settings.first_year - 1, -1)]
    results: dict[str, tuple[str, str] | None] = {reference: None for reference in references}
    max_column = max(settings.lookup_column_index, settings.pdf_column_index, settings.location_column_index)
    for sheet_name in ordered_sheets:
        frame = sheets.get(sheet_name)
        if frame is None or frame.shape[1] <= max_column:
            continue
        lookup = frame.iloc[:, settings.lookup_column_index].map(_cell_text)
        for reference in results:
            if results[reference] is not None:
                continue
            matches = frame.loc[lookup == reference]
            if not matches.empty:
                row = matches.iloc[0]
                pdf_name = _cell_text(row.iloc[settings.pdf_column_index])
                location = _cell_text(row.iloc[settings.location_column_index])
                if pdf_name:
                    results[reference] = (pdf_name, location)
    return results


def process_references(payload: dict[str, Any], settings: Settings) -> dict[str, list[dict[str, str]]]:
    raw_references = payload.get("referencias")
    supplied_terms = payload.get("datosSensibles", [])
    if not isinstance(raw_references, list) or not raw_references:
        raise ValueError("referencias must be a non-empty array")
    references = [str(reference).strip() for reference in raw_references]
    if any(not reference for reference in references):
        raise ValueError("referencias cannot contain empty values")
    if not isinstance(supplied_terms, list) or not all(isinstance(item, str) for item in supplied_terms):
        raise ValueError("datosSensibles must be an array of strings")
    graph = GraphClient(settings)
    try:
        matches = _find_documents(graph.download_excel(), list(dict.fromkeys(references)), settings)
        results: list[dict[str, str]] = []
        for reference in references:
            match = matches[reference]
            if match is None:
                results.append({"referencia": reference, "estado": "Pendiente (No encontrado)"})
                continue
            pdf_name, location = match
            try:
                sanitized = redact_pdf(graph.download_original(pdf_name), [*settings.standard_sensitive_terms, *supplied_terms])
                if len(sanitized) > settings.max_response_pdf_bytes:
                    raise RuntimeError("The sanitized PDF exceeds the configured Base64 response limit")
                results.append({"referencia": reference, "estado": "Exito", "nombre_pdf": pdf_name, "ubicacion": location, "pdf_base64": base64.b64encode(sanitized).decode("ascii")})
            except Exception as exc:
                results.append({"referencia": reference, "estado": "Error", "nombre_pdf": pdf_name, "ubicacion": location, "detalle": str(exc)})
        return {"resultados": results}
    except Exception as exc:
        raise RuntimeError("Batch document processing failed") from exc
