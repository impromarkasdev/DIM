from __future__ import annotations

import io
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pymupdf
import pandas as pd

from .config import Settings
from .errors import PermanentProcessingError
from .errors import GraphAccessDeniedError
from .graph import GraphClient
from .pdf_splitter import FORM_NUMBER, _find_form_number
from .pdf_service import redact_pdf

logger = logging.getLogger(__name__)

# Keep the parsed master index briefly in warm queue workers. Queue messages
# are per reference, so this avoids downloading/parsing the 5 MB workbook for
# every item while still refreshing changes frequently.
_MASTER_INDEX_CACHE_TTL_SECONDS = 90
_MASTER_INDEX_CACHE: dict[tuple[str, str, str, int], tuple[float, dict[str, "DimIndexRow"]]] = {}
_MASTER_INDEX_CACHE_LOCK = threading.Lock()


def _excel_columns(year: str) -> tuple[int, int, int]:
    """Return zero-based (reference, shipment, form) columns for each sheet era."""
    return (42, 43, 44) if int(year) >= 2024 else (41, 42, 43)


def _sheet_excel_columns(
    frame: pd.DataFrame, year: str, column_offset: int = 0
) -> tuple[int, int, int, int]:
    """Resolve Excel columns from their headers, falling back to the known layout.

    The workbook has drifted over time, including sheets whose physical column
    positions do not match the year-based convention. Header names are the
    source of truth when present; the year mapping remains for legacy sheets
    without a recognizable header row.
    """
    expected = {
        "referencia": "reference",
        "embarque": "shipment",
        "numeroformulario": "form",
    }
    scan_rows = min(len(frame.index), 20)
    for row_index in range(scan_rows):
        found: dict[str, list[int]] = {role: [] for role in expected.values()}
        for column_index, value in enumerate(frame.iloc[row_index].tolist()):
            normalized = _plain(_cell_text(value))
            role = expected.get(normalized)
            if role:
                found[role].append(column_index + column_offset)
        if all(found.values()):
            # Some sheets repeat NUMERO_FORMULARIO in a different data block.
            # Choose the closest ordered triplet so all fields come from the
            # same reference/shipment/form block.
            triplets = [
                (reference, shipment, form)
                for reference in found["reference"]
                for shipment in found["shipment"]
                for form in found["form"]
                if reference < shipment < form
            ]
            if triplets:
                reference, shipment, form = min(
                    triplets, key=lambda values: (values[2] - values[0], values[1] - values[0])
                )
                return row_index, reference, shipment, form

    reference_column, shipment_column, form_column = _excel_columns(year)
    return 0, reference_column, shipment_column, form_column


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


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    i = j = differences = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        differences += 1
        if differences > 1:
            return False
        if len(left) > len(right):
            i += 1
        elif len(right) > len(left):
            j += 1
        else:
            i += 1
            j += 1
    if i < len(left) or j < len(right):
        differences += 1
    return differences <= 1


def _filename_form_digits(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    # DIM files are named by Campo 4. Only accept a numeric stem (with the
    # optional leading underscore and check-digit hyphen) so container IDs or
    # arbitrary suffixes cannot become fuzzy matches.
    match = re.fullmatch(r"\s*_?(\d{15})(?:\s*[-‐‑‒–—−]?\s*(\d))?\s*", stem)
    return (match.group(1) + (match.group(2) or "")) if match else ""


def _filename_match_score(filename: str, form_number: str) -> int | None:
    candidate = _filename_form_digits(filename)
    target = "".join(re.findall(r"\d", form_number))
    if not candidate or len(target) not in {15, 16}:
        return None
    if candidate == target:
        return 0
    # The Excel sometimes omits the Campo 4 verification digit.
    if len(target) == 15 and len(candidate) == 16 and candidate.startswith(target):
        return 0
    if len(target) == 16 and len(candidate) == 15 and target.startswith(candidate):
        return 0
    if len(candidate) in {15, 16} and _edit_distance_at_most_one(candidate, target):
        return 1
    return None


def _filename_matches_form(filename: str, form_number: str) -> bool:
    return _filename_match_score(filename, form_number) is not None


def _same_form_number(candidate: str, expected: str) -> bool:
    candidate_key, expected_key = _form_key(candidate), _form_key(expected)
    if candidate_key == expected_key:
        return True
    candidate_digits = "".join(re.findall(r"\d", candidate))
    expected_digits = "".join(re.findall(r"\d", expected))
    return (
        (len(expected_digits) == 15 and len(candidate_digits) == 16 and candidate_digits.startswith(expected_digits))
        or (len(expected_digits) == 16 and len(candidate_digits) == 15 and expected_digits.startswith(candidate_digits))
    )


def _pdf_contains_form(pdf_bytes: bytes, form_number: str) -> bool:
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            found_target = False
            for page in document:
                found = _find_form_number(page.get_text("text"))
                if not found:
                    continue
                if not _same_form_number(found, form_number):
                    # A correctly named file can still be a bad split that
                    # includes the next declaration. Reject it, then use the
                    # consolidated source and extract the exact page range.
                    return False
                found_target = True
            return found_target
    except Exception as exc:
        logger.warning("DIM: no se pudo validar el PDF candidato (%s)", type(exc).__name__)
        return False


def _form_pdf_name(form_number: str, source_name: str = "", pdf_bytes: bytes = b"") -> str:
    """Return the full DIAN Campo 4 name, recovering the check digit if Excel omits it."""
    normalized = re.sub(r"\s+", "", form_number.strip().lstrip("_"))
    match = re.fullmatch(r"(\d{15})[-‐‑‒–—−](\d)", normalized)
    if match:
        return f"{match.group(1)}-{match.group(2)}.pdf"

    # Some workbook cells contain the 15-digit base but omit Campo 4's
    # verification digit. Prefer the matched source filename, then read the
    # actual PDF text (including a declaration extracted from a consolidated).
    digits_match = re.fullmatch(r"(\d{15})(?:\.0)?", normalized)
    if digits_match:
        expected_digits = digits_match.group(1)
        candidates = [source_name]
        if pdf_bytes:
            try:
                with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
                    candidates.extend(page.get_text("text") for page in document)
            except Exception as exc:
                logger.warning("No se pudo recuperar Campo 4 del PDF para nombrarlo: %s", exc)
        for candidate in candidates:
            compact = re.sub(r"\s+", "", candidate.lstrip("_"))
            for found in FORM_NUMBER.finditer(compact):
                found_digits = "".join(re.findall(r"\d", found.group(1)))
                if found_digits == expected_digits:
                    return f"{found_digits}-{found.group(2)}.pdf"

    raise ValueError(
        "El Excel no contiene el dígito verificador del Número de Formulario y no se pudo recuperar del PDF"
    )


def _years(settings: Settings) -> list[str]:
    return [str(year) for year in range(datetime.now().year, settings.first_year - 1, -1)]


def _read_master_index(workbook: bytes, settings: Settings) -> dict[str, DimIndexRow]:
    try:
        wanted_years = set(_years(settings))
        excel = pd.ExcelFile(io.BytesIO(workbook), engine="openpyxl")
        sheets = {
            # The lookup triplet is around AP:AS. Reading a narrow column
            # window avoids materializing all product descriptions.
            name: excel.parse(
                sheet_name=name, header=None, dtype=object, usecols=range(38, 46)
            )
            for name in excel.sheet_names
            if str(name) in wanted_years
        }
    except Exception as exc:
        raise RuntimeError("No fue posible leer el Excel maestro") from exc
    indexed: dict[str, DimIndexRow] = {}
    for year in _years(settings):
        frame = sheets.get(year)
        if frame is None:
            logger.info("DIM Excel: hoja %s no existe", year)
            continue
        column_offset = 38
        header_row, reference_column, shipment_column, form_column = _sheet_excel_columns(
            frame, year, column_offset
        )
        local_columns = tuple(column - column_offset for column in (
            reference_column, shipment_column, form_column
        ))
        if min(local_columns) < 0 or frame.shape[1] <= max(local_columns):
            logger.warning("DIM Excel: hoja %s no contiene las columnas esperadas", year)
            continue
        logger.info(
            "DIM Excel: revisando hoja %s (%s filas; columnas %s/%s/%s; encabezado fila %s)",
            year, len(frame.index) - header_row - 1, reference_column + 1, shipment_column + 1,
            form_column + 1, header_row + 1,
        )
        for column in local_columns:
            frame.iloc[:, column] = frame.iloc[:, column].astype(str).str.strip()
        sheet_matches = 0
        for _, row in frame.iloc[header_row + 1 :].iterrows():
            reference, shipment, form_number = (
                _cell_text(row.iloc[column])
                for column in local_columns
            )
            key = _plain(reference)
            if key and shipment and form_number and key not in indexed:
                indexed[key] = DimIndexRow(reference, year, shipment, form_number)
                sheet_matches += 1
        logger.info("DIM Excel: indexadas %s referencias desde hoja %s", sheet_matches, year)
    logger.info("DIM Excel: índice cargado con %s referencias", len(indexed))
    return indexed


def _master_index(graph: GraphClient, settings: Settings) -> dict[str, DimIndexRow]:
    cache_key = (
        settings.excel_path,
        settings.drive_id,
        settings.graph_user_id,
        settings.first_year,
    )
    now = time.monotonic()
    with _MASTER_INDEX_CACHE_LOCK:
        cached = _MASTER_INDEX_CACHE.get(cache_key)
        if cached and now - cached[0] < _MASTER_INDEX_CACHE_TTL_SECONDS:
            logger.info("DIM Excel: índice maestro reutilizado desde caché caliente")
            return cached[1]

    logger.info("DIM: descargando Excel maestro desde %s", settings.excel_path)
    index = _read_master_index(graph.download_excel(), settings)
    with _MASTER_INDEX_CACHE_LOCK:
        _MASTER_INDEX_CACHE[cache_key] = (time.monotonic(), index)
    return index


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
    scored = [
        (_filename_match_score(str(item.get("name", "")), form_number), item)
        for item in items
        if item.get("file") is not None
        and str(item.get("name", "")).casefold().endswith(".pdf")
        and not str(item.get("name", "")).casefold().startswith(("redactada_", "redacted_"))
    ]
    exact = [item for score, item in scored if score == 0]
    if exact:
        return exact[0]
    approximate = [item for score, item in scored if score == 1]
    # A one-character-tolerant match is safe only when unique in the current
    # search scope; ambiguity must fall through to the consolidated document.
    return approximate[0] if len(approximate) == 1 else None


def _find_consolidated(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in items if item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf") and "dimconlevante" in _plain(str(item.get("name", "")))), None)


def _item_parent_path(item: dict[str, Any]) -> str:
    return GraphClient._parent_path(item.get("parentReference", {}))


def _item_drive_base(graph: GraphClient, item: dict[str, Any]) -> str:
    drive_id = str(item.get("parentReference", {}).get("driveId", ""))
    return f"{graph.base}/drives/{drive_id}" if drive_id else graph.drive_base


def _is_pdf(item: dict[str, Any]) -> bool:
    return item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf")


def _safe_graph_search(graph: GraphClient, query: str, folder_item_id: str) -> list[dict[str, Any]]:
    try:
        return graph.search_items(query, folder_item_id=folder_item_id)
    except GraphAccessDeniedError:
        # Application permissions may allow listing known folders and
        # downloading their contents while denying drive-wide search. That
        # broad search is only an optimization/fallback; continue with the
        # already enumerated container and its DIM CON LEVANTE PDF.
        logger.warning("DIM: búsqueda global de Graph no autorizada; se continúa con la carpeta del contenedor")
        return []
    except Exception as exc:
        # A transient or malformed global search must not become a false miss.
        logger.warning("DIM: búsqueda global de Graph falló (%s)", type(exc).__name__)
        raise


def _global_form_matches(graph: GraphClient, form_number: str, folder_item_id: str) -> list[dict[str, Any]]:
    """Find individually named PDFs anywhere below the configured DIM root."""
    digits = "".join(re.findall(r"\d", form_number))
    matches: dict[str, dict[str, Any]] = {}
    # Graph search may normalize punctuation; validate each result against the
    # canonical form number locally to avoid confusing similar declarations.
    for query in dict.fromkeys((form_number.strip().lstrip("_"), digits)):
        if not query:
            continue
        for item in _safe_graph_search(graph, query, folder_item_id):
            if not _is_pdf(item) or str(item.get("name", "")).casefold().startswith("redactada_"):
                continue
            if _filename_matches_form(str(item.get("name", "")), form_number):
                matches[str(item.get("id", item.get("name", "")))] = item
    return list(matches.values())


def _global_pdf_content_candidates(
    graph: GraphClient, form_number: str, folder_item_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Return indexed PDF hits for the form number, including consolidated files.

    Microsoft Graph search can match file content as well as names. We verify
    candidates by opening their PDF text, and cap the downloads for serverless
    execution time and memory safety.
    """
    digits = "".join(re.findall(r"\d", form_number))
    candidates: dict[str, dict[str, Any]] = {}
    for query in (form_number.strip().lstrip("_"), digits):
        for item in _safe_graph_search(graph, query, folder_item_id):
            if not _is_pdf(item) or str(item.get("name", "")).casefold().startswith("redactada_"):
                continue
            candidates[str(item.get("id", item.get("name", "")))] = item
    # Likely consolidated DIM names first. Exact declaration filenames are
    # handled separately and don't need another download attempt.
    ordered = sorted(
        candidates.values(),
        key=lambda item: ("dim" not in _plain(str(item.get("name", ""))), int(item.get("size", 0) or 0)),
    )
    return ordered[:limit]


def _global_container_paths(graph: GraphClient, shipment: str, folder_item_id: str) -> list[str]:
    """Find shipment folders regardless of year or configured base directory."""
    queries = [shipment.strip()]
    container = re.search(r"\bcont(?:enedor)?\.?\s*(\d+)\b", shipment, re.IGNORECASE)
    if container:
        queries.append(f"Contenedor {container.group(1)}")
    paths: dict[str, None] = {}
    for query in queries:
        for item in _safe_graph_search(graph, query, folder_item_id):
            if item.get("folder") is None or not _shipment_matches(str(item.get("name", "")), shipment):
                continue
            parent = _item_parent_path(item)
            path = f"{parent}/{item['name']}" if parent else str(item["name"])
            paths[path] = None
    return list(paths)


def _global_consolidated_items(
    graph: GraphClient, shipment: str, folder_item_id: str
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for item in _safe_graph_search(graph, "DIM CON LEVANTE", folder_item_id):
        if not _is_pdf(item) or "dimconlevante" not in _plain(str(item.get("name", ""))):
            continue
        segments = _item_parent_path(item).split("/")
        # Keep shipment-matching candidates first and a small bounded fallback
        # in case both the year and the folder naming convention have changed.
        if any(_shipment_matches(segment, shipment) for segment in segments):
            candidates[str(item.get("id", item.get("name", "")))] = item
    return list(candidates.values())


def _walk_scope_for_containers(
    graph: GraphClient,
    root_item_id: str,
    root_path: str,
    shipment: str,
    diagnostics: list[str],
    max_folders: int = 3000,
    max_depth: int = 8,
) -> list[str]:
    """Fallback crawl limited to DIM_BASE_PATH when Graph search is unavailable."""
    pending: list[tuple[str, str, int]] = [(root_item_id, root_path, 0)]
    visited: set[str] = set()
    matches: list[str] = []
    while pending and len(visited) < max_folders:
        item_id, path, depth = pending.pop()
        if item_id in visited:
            continue
        visited.add(item_id)
        try:
            children = graph.list_item_children(item_id)
        except Exception as exc:
            logger.warning("DIM: no se pudo recorrer una carpeta dentro de la raíz autorizada (%s)", type(exc).__name__)
            continue
        for item in children:
            if item.get("folder") is None:
                continue
            name = str(item.get("name", "")).strip()
            child_id = str(item.get("id", "")).strip()
            if not name or not child_id:
                continue
            child_path = f"{path}/{name}" if path else name
            if _shipment_matches(name, shipment):
                matches.append(child_path)
                continue
            if depth < max_depth and len(visited) + len(pending) < max_folders:
                pending.append((child_id, child_path, depth + 1))
    if pending:
        diagnostics.append("El recorrido de respaldo alcanzó su límite de seguridad")
        logger.warning("DIM: se alcanzó el límite de carpetas al recorrer la raíz de documentos")
    if matches:
        diagnostics.append(f"Contenedor localizado mediante recorrido de la carpeta raíz ({len(matches)} coincidencias)")
    return matches


def _extract_declaration(pdf_bytes: bytes, form_number: str) -> bytes:
    target = _form_key(form_number)
    if not target:
        raise RuntimeError("El número de formulario del Excel es inválido")

    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as source:
            selected: list[int] = []
            started = False
            for index, page in enumerate(source):
                text = page.get_text("text")
                # Do not strip whitespace before matching: doing so can merge
                # adjacent field values and make a different declaration look
                # like a continuation of the requested form.
                field_form = _find_form_number(text)
                if field_form:
                    if _same_form_number(field_form, form_number):
                        started = True
                        selected.append(index)
                    elif started:
                        break
                    continue

                if not started:
                    continue

                # Continuation sheets identify the declaration under
                # "Aceptación" rather than Campo 4. Use only that labeled
                # value as a boundary; other long numbers elsewhere on the
                # page are not declaration identifiers.
                acceptance = re.search(r"Aceptaci[oó]n\s*:?\s*", text, re.IGNORECASE)
                if acceptance:
                    tail = text[acceptance.end():acceptance.end() + 180]
                    acceptance_form = FORM_NUMBER.search(tail)
                    if acceptance_form and not _same_form_number(acceptance_form.group(0), form_number):
                        break
                selected.append(index)

            if not selected:
                raise RuntimeError("El número de formulario no apareció en el DIM consolidado")
            with pymupdf.open() as result:
                for index in selected:
                    result.insert_pdf(source, from_page=index, to_page=index)
                for page in result:
                    extracted_form = _find_form_number(page.get_text("text"))
                    if extracted_form and not _same_form_number(extracted_form, form_number):
                        raise RuntimeError("La extracción incluyó una página de otra declaración")
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
    # Most entries are in the year shown by Excel. Check that folder and the
    # root-level legacy location first. Only fan out across other years when
    # neither contains the shipment.
    parents = [f"{settings.dim_base_path}/{entry.year}", settings.dim_base_path]
    found: list[str] = []
    seen: set[str] = set()
    def inspect(parent: str) -> None:
        if parent in seen:
            return
        seen.add(parent)
        try:
            items = graph.list_children(parent)
        except Exception as exc:
            diagnostics.append(f"No se pudo listar: {parent.rsplit('/', 1)[-1]}")
            logger.warning("DIM: no se pudo listar una carpeta candidata (%s)", type(exc).__name__)
            return
        matches = [item for item in items if item.get("folder") is not None and _shipment_matches(str(item.get("name", "")), entry.shipment)]
        for item in matches:
            path = f"{parent}/{item['name']}"
            if path not in found:
                found.append(path)
                diagnostics.append(f"Contenedor encontrado: {item['name']}")

    for parent in parents:
        inspect(parent)
    if not found:
        for year in _years(settings):
            if year != entry.year:
                inspect(f"{settings.dim_base_path}/{year}")
    return found


def _walk_container(graph: GraphClient, container_path: str, diagnostics: list[str], max_depth: int = 6) -> list[tuple[str, list[dict[str, Any]]]]:
    """List only folders that actually exist, prioritizing Procesados then DIM."""
    queue: list[tuple[str, int, int, str | None]] = [(container_path, 0, 0, None)]
    listings: list[tuple[str, list[dict[str, Any]]]] = []
    visited: set[str] = set()
    while queue:
        queue.sort(key=lambda entry: (entry[2], entry[1]))
        path, depth, _priority, item_id = queue.pop(0)
        if path in visited or depth > max_depth:
            continue
        visited.add(path)
        try:
            # Resolve the top-level container by path once; descend using stable
            # DriveItem IDs so names with &, accents or SharePoint path quirks
            # cannot make the first lookup fail intermittently.
            items = graph.list_item_children(item_id) if item_id else graph.list_children(path)
        except Exception:
            logger.info("DIM: carpeta no disponible %s", path)
            continue
        listings.append((path, items))
        for item in items:
            if item.get("folder") is not None:
                folder_name = str(item.get("name", ""))
                if folder_name.casefold() in {"enviados", "redactados"}:
                    continue
                folded = folder_name.casefold()
                priority = 0 if folded == "procesados" else 1 if folded == "dim" else 2
                child_id = str(item.get("id", "")).strip() or None
                if child_id:
                    queue.append((f"{path}/{folder_name}", depth + 1, priority, child_id))
    diagnostics.append(f"Se exploraron {len(listings)} carpetas del contenedor")
    return listings


def _download_validated_candidate(
    graph: GraphClient, item: dict[str, Any], form_number: str, parent_path: str
) -> LocatedPdf | None:
    content = graph.download_item(item)
    if not _pdf_contains_form(content, form_number):
        logger.warning("DIM: se descartó un PDF cuyo nombre coincidía, pero Campo 4 no coincide")
        return None
    return LocatedPdf(content, str(item.get("name", "documento.pdf")), parent_path, _item_drive_base(graph, item))


def _search_container(
    graph: GraphClient,
    container_path: str,
    form_number: str,
    diagnostics: list[str],
    max_depth: int = 6,
) -> tuple[LocatedPdf | None, list[tuple[dict[str, Any], str]]]:
    """Search Processed first, then this container only; return DIM consolidations last."""
    root = graph.get_item_by_path(container_path)
    root_id = str(root.get("id", ""))
    if not root_id or root.get("folder") is None:
        return None, []

    listed: dict[str, list[dict[str, Any]]] = {}

    def children(item_id: str) -> list[dict[str, Any]]:
        if item_id not in listed:
            listed[item_id] = graph.list_item_children(item_id)
        return listed[item_id]

    root_children = children(root_id)
    root_processed = [
        (item, f"{container_path}/{item.get('name', 'Procesados')}")
        for item in root_children
        if item.get("folder") is not None and str(item.get("name", "")).casefold() == "procesados"
    ]

    # Stage 1: processed declarations. This short-circuits before traversing
    # unrelated subfolders, which is the normal/fast path for repeat requests.
    def search_processed(folders: list[tuple[dict[str, Any], str]]) -> LocatedPdf | None:
        exact: list[tuple[dict[str, Any], str]] = []
        fuzzy_matches: list[tuple[dict[str, Any], str]] = []
        for folder, folder_path in folders:
            folder_id = str(folder.get("id", ""))
            if not folder_id:
                continue
            for item in children(folder_id):
                score = _filename_match_score(str(item.get("name", "")), form_number)
                if score == 0 and _is_pdf(item):
                    exact.append((item, folder_path))
                elif score == 1 and _is_pdf(item):
                    fuzzy_matches.append((item, folder_path))
        for item, parent in exact:
            located = _download_validated_candidate(graph, item, form_number, parent)
            if located:
                diagnostics.append("Declaración validada en Procesados")
                return located
        if len(fuzzy_matches) == 1:
            item, parent = fuzzy_matches[0]
            located = _download_validated_candidate(graph, item, form_number, parent)
            if located:
                diagnostics.append("Declaración validada en Procesados con una diferencia de nombre")
                return located
        return None

    located = search_processed(root_processed)
    if located:
        return located, []

    # The splitter stores results beside the master PDF, commonly under
    # /DIM/Procesados. Only inspect that second canonical location if the
    # container-root Procesados folder did not contain the declaration.
    dim_folders = [
        item for item in root_children
        if item.get("folder") is not None and str(item.get("name", "")).casefold() == "dim"
    ]
    dim_processed: list[tuple[dict[str, Any], str]] = []
    for dim in dim_folders:
        dim_id = str(dim.get("id", ""))
        if not dim_id:
            continue
        dim_path = f"{container_path}/{dim.get('name', 'DIM')}"
        for item in children(dim_id):
            if item.get("folder") is not None and str(item.get("name", "")).casefold() == "procesados":
                dim_processed.append((item, f"{dim_path}/{item.get('name', 'Procesados')}"))
    located = search_processed(dim_processed)
    if located:
        return located, []

    # Stage 2: files with the form number anywhere inside this container.
    # Use DriveItem IDs and memoized listings so DIM/Procesados is not queried
    # twice. Procesados is skipped here because it was already checked.
    queue: list[tuple[str, str, int]] = [(container_path, root_id, 0)]
    visited: set[str] = set()
    fuzzy: dict[str, tuple[dict[str, Any], str]] = {}
    consolidated: dict[str, tuple[dict[str, Any], str]] = {}
    visited_count = 0
    while queue and visited_count < 3000:
        path, item_id, depth = queue.pop(0)
        if item_id in visited or depth > max_depth:
            continue
        visited.add(item_id)
        visited_count += 1
        try:
            items = children(item_id)
        except Exception as exc:
            logger.warning("DIM: no se pudo listar una subcarpeta del contenedor (%s)", type(exc).__name__)
            continue

        for item in items:
            if _is_pdf(item):
                name = str(item.get("name", ""))
                if name.casefold().startswith(("redactada_", "redacted_")):
                    continue
                score = _filename_match_score(name, form_number)
                if score == 0:
                    located = _download_validated_candidate(graph, item, form_number, path)
                    if located:
                        diagnostics.append("PDF individual validado dentro del contenedor")
                        diagnostics.append(f"Se revisaron {visited_count} carpetas del contenedor")
                        return located, []
                elif score == 1:
                    fuzzy[str(item.get("id", name))] = (item, path)
                elif "dimconlevante" in _plain(name):
                    consolidated[str(item.get("id", name))] = (item, path)
                continue

            if item.get("folder") is None:
                continue
            child_name = str(item.get("name", "")).strip()
            child_id = str(item.get("id", "")).strip()
            folded = child_name.casefold()
            if not child_name or not child_id or folded in {"procesados", "enviados", "redactados"}:
                continue
            if depth < max_depth:
                queue.append((f"{path}/{child_name}", child_id, depth + 1))

    if visited_count >= 3000 and queue:
        diagnostics.append("El recorrido del contenedor alcanzó el límite de seguridad")
        logger.warning("DIM: recorrido del contenedor detenido en el límite de 3000 carpetas")

    if len(fuzzy) == 1:
        item, parent = next(iter(fuzzy.values()))
        located = _download_validated_candidate(graph, item, form_number, parent)
        if located:
            diagnostics.append("PDF individual validado con una diferencia de nombre")
            diagnostics.append(f"Se revisaron {visited_count} carpetas del contenedor")
            return located, []
    elif fuzzy:
        logger.warning("DIM: hay varios nombres de PDF aproximados; se evita elegir uno ambiguo")

    diagnostics.append(f"Se revisaron {visited_count} carpetas del contenedor")
    consolidated_items = sorted(
        consolidated.values(),
        key=lambda pair: ("/dim/" not in (pair[1].casefold() + "/"), int(pair[0].get("size", 0) or 0)),
    )
    return None, consolidated_items


def _download_hierarchical_pdf(graph: GraphClient, entry: DimIndexRow, settings: Settings, diagnostics: list[str]) -> LocatedPdf:
    root_item = graph.get_item_by_path(settings.dim_base_path)
    if root_item.get("folder") is None:
        raise RuntimeError("La ruta DIM_BASE_PATH configurada no corresponde a una carpeta")
    diagnostics.append("Búsqueda limitada a la carpeta base de DIM y al contenedor indicado por Excel")
    containers = _container_paths(graph, entry, settings, diagnostics)
    if not containers:
        root_item_id = str(root_item["id"])
        # This bounded crawl is only a fallback for installations where the
        # year/location convention changed. The normal path uses a few direct
        # folder listings and never invokes the permission-denied drive search.
        containers = _walk_scope_for_containers(
            graph, root_item_id, settings.dim_base_path, entry.shipment, diagnostics,
            max_folders=1200, max_depth=6,
        )
    if not containers:
        raise RuntimeError("No se encontró el contenedor indicado dentro de la carpeta DIM configurada")

    consolidations: list[tuple[dict[str, Any], str]] = []
    for container_path in containers:
        logger.info("DIM: etapa 1 Procesados; etapa 2 archivos del contenedor")
        located, candidates = _search_container(
            graph, container_path, entry.form_number, diagnostics
        )
        if located:
            return located
        consolidations.extend(candidates)

    # Stage 3: only after Processed and all files in the matching container
    # have been checked, extract the requested declaration from DIM CON LEVANTE.
    for consolidated, parent in consolidations:
        logger.info("DIM: etapa 3, extracción puntual desde DIM CON LEVANTE")
        try:
            source_bytes = graph.download_item(consolidated)
            extracted = _extract_declaration(source_bytes, entry.form_number)
        except RuntimeError as exc:
            logger.info("DIM: el consolidado candidato no contiene la declaración solicitada (%s)", type(exc).__name__)
            continue

        file_name = _form_pdf_name(entry.form_number, str(consolidated.get("name", "")), extracted)
        drive_base = _item_drive_base(graph, consolidated)
        processed_path = f"{parent}/Procesados"
        try:
            graph.ensure_folder(processed_path, drive_base)
            saved = graph.upload_pdf(f"{processed_path}/{file_name}", extracted, drive_base)
            if not saved.get("id"):
                raise RuntimeError("El documento separado no devolvió un identificador de OneDrive")
            diagnostics.append("Declaración exacta extraída del DIM con Levante y preparada en Procesados")
        except Exception as exc:
            # The extracted bytes are already verified. If the optional cache
            # copy cannot be saved, continue redacting and returning this exact
            # in-memory document instead of turning a storage hiccup into a miss.
            logger.warning("DIM: no se pudo guardar la extracción en Procesados (%s)", type(exc).__name__)
            diagnostics.append("La extracción se procesó en memoria porque no se pudo guardar la copia en Procesados")
        return LocatedPdf(extracted, file_name, processed_path, drive_base, True)

    raise RuntimeError("No se encontró la declaración en Procesados, el contenedor ni DIM CON LEVANTE")


def process_hierarchical_references(references: list[str], supplied_terms: list[str], settings: Settings) -> dict[str, list[dict[str, Any]]]:
    if not references or any(not isinstance(value, str) or not value.strip() for value in references):
        raise ValueError("references must be a non-empty array of strings")
    if not isinstance(supplied_terms, list) or not all(isinstance(value, str) for value in supplied_terms):
        raise ValueError("datosSensibles must be an array of strings")
    graph = GraphClient(settings)
    index = _master_index(graph, settings)
    # The target casillas are always redacted. Only request-specific terms are
    # supplemental; generic defaults such as "nit" must not mask field labels.
    terms = [value.strip() for value in supplied_terms if value.strip()]
    results: list[dict[str, Any]] = []
    for raw_reference in references:
        reference, diagnostics = raw_reference.strip(), ["Excel maestro descargado"]
        entry = index.get(_plain(reference))
        if not entry:
            logger.info("DIM: referencia no encontrada en Excel")
            results.append({"referencia": reference, "estado": "Pendiente (No encontrado)", "diagnostico": diagnostics})
            continue
        diagnostics.append(f"Referencia encontrada en hoja {entry.year}")
        try:
            located: LocatedPdf | None = None
            for lookup_attempt in range(2):
                try:
                    located = _download_hierarchical_pdf(graph, entry, settings, diagnostics)
                    break
                except RuntimeError:
                    # Folder listing/search code deliberately tolerates an
                    # individual inaccessible location. If all candidates miss
                    # after Graph reported a transient failure, repeat the
                    # hierarchy once within this request instead of asking the
                    # Wix user to click Search a second time.
                    if lookup_attempt > 0 or not graph.had_transient_failure:
                        raise
                    graph.had_transient_failure = False
                    diagnostics.append("Se reintentó la búsqueda por una respuesta temporal de SharePoint")
                    logger.warning("DIM: repitiendo búsqueda tras un fallo temporal de Graph")
            if located is None:
                raise RuntimeError("No fue posible resolver el archivo de la declaración")
            sanitized = redact_pdf(located.content, terms)
            file_name = _form_pdf_name(entry.form_number, located.file_name, located.content)
            source_folder = located.parent_folder
            if source_folder.endswith("/Procesados"):
                source_folder = source_folder.rsplit("/", 1)[0]
            output_folder = f"{source_folder}/Enviados"
            metadata = graph.upload_pdf(f"{output_folder}/{file_name}", sanitized, located.drive_base)
            diagnostics.append("Redacción física aplicada y archivo subido")
            if not metadata.get("id"):
                raise RuntimeError("Microsoft Graph guardó el PDF sin devolver su identificador")
            drive_reference = metadata.get("parentReference", {}).get("driveId")
            stable_drive_base = (
                f"{graph.base}/drives/{drive_reference}" if drive_reference else located.drive_base
            )
            results.append({
                "referencia": reference,
                "estado": "Exito",
                "nombre_pdf": file_name,
                "embarque": entry.shipment,
                "numeroFormulario": entry.form_number,
                "outputFolder": f"/{output_folder}",
                "downloadFiles": [{
                    "driveItemId": str(metadata["id"]),
                    "driveBase": stable_drive_base,
                    "name": file_name,
                }],
                "extraidoDeConsolidado": located.extracted_from_consolidated,
                "diagnostico": diagnostics,
            })
        except PermanentProcessingError as exc:
            logger.error("DIM: redacción rechazada por una verificación permanente")
            results.append({
                "referencia": reference,
                "estado": "Error",
                "detalle": str(exc),
                "diagnostico": diagnostics,
                "_permanentError": True,
            })
        except Exception as exc:
            logger.exception("DIM: fallo procesando una referencia")
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
