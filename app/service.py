from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import fitz
import pandas as pd

from .config import Settings
from .graph import GraphClient
from .pdf_splitter import FORM_NUMBER
from .pdf_service import redact_pdf

logger = logging.getLogger(__name__)
REFERENCE_COLUMN, SHIPMENT_COLUMN, FORM_COLUMN = 41, 42, 43  # AP, AQ, AR (zero based)


@dataclass(frozen=True)
class DimIndexRow:
    reference: str
    year: str
    shipment: str
    form_number: str


@dataclass(frozen=True)
class LocatedPdf:
    content: bytes
    file_name: str
    parent_folder: str
    drive_base: str
    extracted_from_consolidated: bool = False


def _cell_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "<na>"} else text


def _plain(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold()) if char.isalnum())


def _form_key(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _years(settings: Settings) -> list[str]:
    return [str(year) for year in range(datetime.now().year, settings.first_year - 1, -1)]


def _read_master_index(workbook: bytes, settings: Settings) -> dict[str, DimIndexRow]:
    try:
        sheets = pd.read_excel(io.BytesIO(workbook), sheet_name=None, header=None, dtype=object, engine="openpyxl")
    except Exception as exc:
        raise RuntimeError("No fue posible leer el Excel maestro") from exc
    indexed: dict[str, DimIndexRow] = {}
    for year in _years(settings):
        frame = sheets.get(year)
        if frame is None:
            logger.info("DIM Excel: hoja %s no existe", year)
            continue
        if frame.shape[1] <= FORM_COLUMN:
            logger.warning("DIM Excel: hoja %s no contiene las columnas AP:AQ:AR", year)
            continue
        logger.info("DIM Excel: revisando hoja %s (%s filas)", year, len(frame.index))
        for column in (REFERENCE_COLUMN, SHIPMENT_COLUMN, FORM_COLUMN):
            frame.iloc[:, column] = frame.iloc[:, column].astype(str).str.strip()
        sheet_matches = 0
        for _, row in frame.iterrows():
            reference, shipment, form_number = (_cell_text(row.iloc[column]) for column in (REFERENCE_COLUMN, SHIPMENT_COLUMN, FORM_COLUMN))
            key = _plain(reference)
            if key and shipment and form_number and key not in indexed:
                indexed[key] = DimIndexRow(reference, year, shipment, form_number)
                sheet_matches += 1
        logger.info("DIM Excel: indexadas %s referencias desde hoja %s", sheet_matches, year)
    logger.info("DIM Excel: índice cargado con %s referencias", len(indexed))
    return indexed


def _shipment_matches(folder_name: str, shipment: str) -> bool:
    normalized_folder, normalized_shipment = _plain(folder_name), _plain(shipment)
    if normalized_shipment and normalized_shipment in normalized_folder:
        return True
    shipment_container = re.search(r"\bcont(?:enedor)?\.?\s*(\d+)\b", shipment, re.IGNORECASE)
    folder_container = re.search(r"^\s*cont(?:enedor)?\.?\s*(\d+)\b", folder_name, re.IGNORECASE)
    if shipment_container and folder_container:
        # Prefer the explicit leading container number. Do not confuse it with
        # an import/order number later in names such as "Contenedor 145 - IMP2026-146".
        return shipment_container.group(1) == folder_container.group(1)
    if shipment_container:
        return bool(re.search(
            rf"\bcont(?:enedor)?\.?\s*0*{re.escape(shipment_container.group(1))}\b",
            folder_name,
            re.IGNORECASE,
        ))
    numbers = re.findall(r"\d+", shipment)
    return bool(numbers and all(re.search(rf"(?<!\d){re.escape(number)}(?!\d)", folder_name) for number in numbers))


def _find_pdf(items: list[dict[str, Any]], form_number: str) -> dict[str, Any] | None:
    target = _form_key(form_number)
    return next((item for item in items if item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf") and target in _form_key(str(item.get("name", "")))), None)


def _find_consolidated(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in items if item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf") and "dimconlevante" in _plain(str(item.get("name", "")))), None)


def _extract_declaration(pdf_bytes: bytes, form_number: str) -> bytes:
    target = _form_key(form_number)
    if not target:
        raise RuntimeError("El número de formulario del Excel es inválido")
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as source:
            selected: list[int] = []
            started = False
            for index, page in enumerate(source):
                text = page.get_text("text")
                compact_text = re.sub(r"\s+", "", text)
                forms = {match.group(0) for match in FORM_NUMBER.finditer(compact_text)}
                if target in _form_key(compact_text):
                    started = True
                    selected.append(index)
                elif started:
                    if any(_form_key(value) != target for value in forms):
                        break
                    selected.append(index)  # preserve blank / continuation pages
            if not selected:
                raise RuntimeError("El número de formulario no apareció en el DIM consolidado")
            with fitz.open() as result:
                for index in selected:
                    result.insert_pdf(source, from_page=index, to_page=index)
                result.set_metadata({})
                return result.tobytes(garbage=4, deflate=True, clean=True)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("No fue posible extraer la declaración del DIM consolidado") from exc


def _container_paths(graph: GraphClient, entry: DimIndexRow, settings: Settings, diagnostics: list[str]) -> list[str]:
    """Find matching containers below the expected year and directly at base.

    Some historic imports were placed alongside year folders.  Looking at the
    base itself after the year folders makes those imports first-class without
    weakening the match to an unrelated declaration.
    """
    parents = [f"{settings.dim_base_path}/{year}" for year in [entry.year] + [year for year in _years(settings) if year != entry.year]]
    parents.append(settings.dim_base_path)
    found: list[str] = []
    seen: set[str] = set()
    for parent in parents:
        if parent in seen:
            continue
        seen.add(parent)
        try:
            items = graph.list_children(parent)
        except Exception as exc:
            diagnostics.append(f"No se pudo listar: {parent.rsplit('/', 1)[-1]}")
            logger.warning("DIM %s: no se pudo listar %s: %s", entry.reference, parent, exc)
            continue
        matches = [item for item in items if item.get("folder") is not None and _shipment_matches(str(item.get("name", "")), entry.shipment)]
        for item in matches:
            path = f"{parent}/{item['name']}"
            if path not in found:
                found.append(path)
                diagnostics.append(f"Contenedor encontrado: {item['name']}")
    return found


def _walk_container(graph: GraphClient, container_path: str, diagnostics: list[str], max_depth: int = 6) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return all accessible folder listings in priority-aware breadth-first order."""
    priority = [f"{container_path}/Procesados", f"{container_path}/DIM/Procesados", f"{container_path}/DIM", container_path]
    queue: list[tuple[str, int]] = [(path, path.count("/") - container_path.count("/")) for path in priority]
    listings: list[tuple[str, list[dict[str, Any]]]] = []
    visited: set[str] = set()
    while queue:
        path, depth = queue.pop(0)
        if path in visited or depth > max_depth:
            continue
        visited.add(path)
        try:
            items = graph.list_children(path)
        except Exception:
            logger.info("DIM: carpeta no disponible %s", path)
            continue
        listings.append((path, items))
        for item in items:
            if item.get("folder") is not None:
                queue.append((f"{path}/{item['name']}", depth + 1))
    diagnostics.append(f"Se exploraron {len(listings)} carpetas del contenedor")
    return listings


def _download_hierarchical_pdf(graph: GraphClient, entry: DimIndexRow, settings: Settings, diagnostics: list[str]) -> LocatedPdf:
    containers = _container_paths(graph, entry, settings, diagnostics)
    if not containers:
        diagnostics.append("Sin contenedor coincidente en carpetas anuales ni raíz")
    for container_path in containers:
        logger.info("DIM %s: explorando contenedor %s", entry.reference, container_path)
        listings = _walk_container(graph, container_path, diagnostics)
        # Prefer a generated /Procesados PDF, but accept an exact matching PDF
        # from any nested location in the container before falling back.
        for path, items in listings:
            direct = _find_pdf(items, entry.form_number)
            if direct:
                diagnostics.append("PDF individual encontrado")
                return LocatedPdf(graph.download_path(f"{path}/{direct['name']}"), str(direct["name"]), path, graph.drive_base)
        for path, items in listings:
            consolidated = _find_consolidated(items)
            if consolidated:
                diagnostics.append("PDF individual ausente; se usó DIM consolidado")
                extracted = _extract_declaration(graph.download_path(f"{path}/{consolidated['name']}"), entry.form_number)
                return LocatedPdf(extracted, f"{entry.form_number.lstrip('_')}.pdf", path, graph.drive_base, True)
    raise RuntimeError("No se encontró el contenedor o la declaración indicada en SharePoint")


def process_hierarchical_references(references: list[str], supplied_terms: list[str], settings: Settings) -> dict[str, list[dict[str, Any]]]:
    if not references or any(not isinstance(value, str) or not value.strip() for value in references):
        raise ValueError("references must be a non-empty array of strings")
    if not isinstance(supplied_terms, list) or not all(isinstance(value, str) for value in supplied_terms):
        raise ValueError("datosSensibles must be an array of strings")
    graph = GraphClient(settings)
    logger.info("DIM: descargando Excel maestro desde %s", settings.excel_path)
    index = _read_master_index(graph.download_excel(), settings)
    terms = [*settings.standard_sensitive_terms, *[value.strip() for value in supplied_terms if value.strip()]]
    if not terms:
        raise ValueError("Configure STANDARD_SENSITIVE_TERMS or send datosSensibles for physical redaction")
    results: list[dict[str, Any]] = []
    for raw_reference in references:
        reference, diagnostics = raw_reference.strip(), ["Excel maestro descargado"]
        entry = index.get(_plain(reference))
        if not entry:
            logger.info("DIM: referencia %s no encontrada en Excel", reference)
            results.append({"referencia": reference, "estado": "Pendiente (No encontrado)", "diagnostico": diagnostics})
            continue
        diagnostics.append(f"Referencia encontrada en hoja {entry.year}")
        try:
            located = _download_hierarchical_pdf(graph, entry, settings, diagnostics)
            sanitized = redact_pdf(located.content, terms)
            file_name = f"redactada_{located.file_name.lstrip('_')}"
            output_folder = located.parent_folder if located.parent_folder.endswith("/Procesados") else f"{located.parent_folder}/Procesados"
            metadata = graph.upload_pdf(f"{output_folder}/{file_name}", sanitized, located.drive_base)
            diagnostics.append("Redacción física aplicada y archivo subido")
            results.append({"referencia": reference, "estado": "Exito", "nombre_pdf": file_name, "embarque": entry.shipment, "numeroFormulario": entry.form_number, "outputFolder": f"/{output_folder}", "downloadUrl": metadata.get("@microsoft.graph.downloadUrl", ""), "extraidoDeConsolidado": located.extracted_from_consolidated, "diagnostico": diagnostics})
        except Exception as exc:
            logger.exception("DIM: fallo procesando referencia %s", reference)
            results.append({"referencia": reference, "estado": "Error", "detalle": str(exc), "diagnostico": diagnostics})
    return {"resultados": results}


def process_references(payload: dict[str, Any], settings: Settings) -> dict[str, list[dict[str, Any]]]:
    """Compatibility entry point retained for the protected legacy webhook."""
    references = payload.get("referencias", payload.get("references"))
    if isinstance(references, str):
        references = [references]
    terms = payload.get("datosSensibles", [])
    if not isinstance(references, list):
        raise ValueError("referencias must be a non-empty array")
    if not isinstance(terms, list):
        raise ValueError("datosSensibles must be an array of strings")
    return process_hierarchical_references(references, terms, settings)
