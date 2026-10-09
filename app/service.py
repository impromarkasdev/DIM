from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pymupdf
import pandas as pd

from .config import Settings
from .graph import GraphClient
from .pdf_splitter import FORM_NUMBER
from .pdf_service import redact_pdf

logger = logging.getLogger(__name__)


def _excel_columns(year: str) -> tuple[int, int, int]:
    """Return zero-based (reference, shipment, form) columns for each sheet era."""
    return (42, 43, 44) if int(year) >= 2024 else (41, 42, 43)


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
        sheets = pd.read_excel(io.BytesIO(workbook), sheet_name=None, header=None, dtype=object, engine="openpyxl")
    except Exception as exc:
        raise RuntimeError("No fue posible leer el Excel maestro") from exc
    indexed: dict[str, DimIndexRow] = {}
    for year in _years(settings):
        frame = sheets.get(year)
        if frame is None:
            logger.info("DIM Excel: hoja %s no existe", year)
            continue
        reference_column, shipment_column, form_column = _excel_columns(year)
        if frame.shape[1] <= form_column:
            logger.warning("DIM Excel: hoja %s no contiene las columnas esperadas", year)
            continue
        logger.info(
            "DIM Excel: revisando hoja %s (%s filas; columnas %s/%s/%s)",
            year, len(frame.index), reference_column + 1, shipment_column + 1, form_column + 1,
        )
        for column in (reference_column, shipment_column, form_column):
            frame.iloc[:, column] = frame.iloc[:, column].astype(str).str.strip()
        sheet_matches = 0
        for _, row in frame.iterrows():
            reference, shipment, form_number = (
                _cell_text(row.iloc[column])
                for column in (reference_column, shipment_column, form_column)
            )
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
    candidates = [
        item for item in items
        if item.get("file") is not None
        and str(item.get("name", "")).casefold().endswith(".pdf")
        and not str(item.get("name", "")).casefold().startswith("redactada_")
        and target in _form_key(str(item.get("name", "")))
    ]
    # Prefer the exact split declaration over any file whose name merely
    # contains the form number.
    return next(
        (item for item in candidates if _form_key(str(item.get("name", "")).rsplit(".", 1)[0]) == target),
        candidates[0] if candidates else None,
    )


def _find_consolidated(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in items if item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf") and "dimconlevante" in _plain(str(item.get("name", "")))), None)


def _item_parent_path(item: dict[str, Any]) -> str:
    return GraphClient._parent_path(item.get("parentReference", {}))


def _item_drive_base(graph: GraphClient, item: dict[str, Any]) -> str:
    drive_id = str(item.get("parentReference", {}).get("driveId", ""))
    return f"{graph.base}/drives/{drive_id}" if drive_id else graph.drive_base


def _is_pdf(item: dict[str, Any]) -> bool:
    return item.get("file") is not None and str(item.get("name", "")).casefold().endswith(".pdf")


def _safe_graph_search(graph: GraphClient, query: str) -> list[dict[str, Any]]:
    try:
        return graph.search_items(query)
    except Exception as exc:
        # Do not turn an unavailable or incomplete global search into a false
        # "not found" result. Let the job retry or report a service failure.
        logger.warning("DIM: búsqueda global de Graph falló (%s)", type(exc).__name__)
        raise


def _global_form_matches(graph: GraphClient, form_number: str) -> list[dict[str, Any]]:
    """Find individually named declaration PDFs anywhere in the selected drive."""
    digits = "".join(re.findall(r"\d", form_number))
    matches: dict[str, dict[str, Any]] = {}
    # Graph search may normalize punctuation; validate each result against the
    # canonical form number locally to avoid confusing similar declarations.
    for query in (form_number.strip().lstrip("_"), digits):
        if not query:
            continue
        for item in _safe_graph_search(graph, query):
            if not _is_pdf(item) or str(item.get("name", "")).casefold().startswith("redactada_"):
                continue
            name_key = _form_key(str(item.get("name", "")).rsplit(".", 1)[0])
            if name_key == _form_key(form_number):
                matches[str(item.get("id", item.get("name", "")))] = item
    return list(matches.values())


def _global_pdf_content_candidates(graph: GraphClient, form_number: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return indexed PDF hits for the form number, including consolidated files.

    Microsoft Graph search can match file content as well as names. We verify
    candidates by opening their PDF text, and cap the downloads for serverless
    execution time and memory safety.
    """
    digits = "".join(re.findall(r"\d", form_number))
    candidates: dict[str, dict[str, Any]] = {}
    for query in (form_number.strip().lstrip("_"), digits):
        for item in _safe_graph_search(graph, query):
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


def _global_container_paths(graph: GraphClient, shipment: str) -> list[str]:
    """Find shipment folders regardless of year or configured base directory."""
    queries = [shipment.strip()]
    container = re.search(r"\bcont(?:enedor)?\.?\s*(\d+)\b", shipment, re.IGNORECASE)
    if container:
        queries.append(f"Contenedor {container.group(1)}")
    paths: dict[str, None] = {}
    for query in queries:
        for item in _safe_graph_search(graph, query):
            if item.get("folder") is None or not _shipment_matches(str(item.get("name", "")), shipment):
                continue
            parent = _item_parent_path(item)
            path = f"{parent}/{item['name']}" if parent else str(item["name"])
            paths[path] = None
    return list(paths)


def _global_consolidated_items(graph: GraphClient, shipment: str) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for item in _safe_graph_search(graph, "DIM CON LEVANTE"):
        if not _is_pdf(item) or "dimconlevante" not in _plain(str(item.get("name", ""))):
            continue
        segments = _item_parent_path(item).split("/")
        # Keep shipment-matching candidates first and a small bounded fallback
        # in case both the year and the folder naming convention have changed.
        if any(_shipment_matches(segment, shipment) for segment in segments):
            candidates[str(item.get("id", item.get("name", "")))] = item
    return list(candidates.values())


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
            with pymupdf.open() as result:
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


def _download_hierarchical_pdf(graph: GraphClient, entry: DimIndexRow, settings: Settings, diagnostics: list[str]) -> LocatedPdf:
    containers = _container_paths(graph, entry, settings, diagnostics)
    if not containers:
        diagnostics.append("Sin contenedor coincidente en carpetas anuales ni raíz")

    # Search every known shipment folder before falling back to any consolidated
    # file. Missing /Procesados and /DIM directories are normal and are skipped.
    all_listings: list[tuple[str, list[dict[str, Any]]]] = []
    for container_path in containers:
        logger.info("DIM: explorando contenedor candidato")
        all_listings.extend(_walk_container(graph, container_path, diagnostics))

    # The listing order gives /Procesados precedence, followed by the rest of
    # the container tree (including DIM/ and any arbitrary subfolder).
    for path, items in all_listings:
        direct = _find_pdf(items, entry.form_number)
        if direct:
            diagnostics.append("PDF individual encontrado en carpeta del contenedor")
            return LocatedPdf(graph.download_item(direct), str(direct["name"]), path, _item_drive_base(graph, direct))

    # The form number is the strongest key in Excel. Search the whole selected
    # drive before concluding that a missing folder or /Procesados means 404.
    logger.info("DIM: buscando formulario exacto en todo el drive")
    for direct in _global_form_matches(graph, entry.form_number):
        parent = _item_parent_path(direct)
        diagnostics.append("PDF individual encontrado mediante búsqueda global del drive")
        return LocatedPdf(
            graph.download_item(direct), str(direct.get("name", "documento.pdf")), parent,
            _item_drive_base(graph, direct),
        )

    # Folder names can move between year directories or the configured base.
    # Graph search locates shipment folder candidates independently of either.
    known_paths = set(containers)
    for container_path in _global_container_paths(graph, entry.shipment):
        if container_path in known_paths:
            continue
        logger.info("DIM: explorando contenedor localizado por búsqueda global")
        global_listings = _walk_container(graph, container_path, diagnostics)
        all_listings.extend(global_listings)
        for path, items in global_listings:
            direct = _find_pdf(items, entry.form_number)
            if direct:
                diagnostics.append("PDF individual encontrado dentro del contenedor localizado globalmente")
                return LocatedPdf(graph.download_item(direct), str(direct["name"]), path, _item_drive_base(graph, direct))

    # Only after all individual PDFs have been checked, use a consolidated DIM.
    for path, items in all_listings:
        consolidated = _find_consolidated(items)
        if consolidated:
            diagnostics.append("PDF individual ausente; se usó DIM consolidado")
            extracted = _extract_declaration(graph.download_item(consolidated), entry.form_number)
            return LocatedPdf(extracted, f"{entry.form_number.lstrip('_')}.pdf", path, _item_drive_base(graph, consolidated), True)

    # Last resort: search all indexed consolidated DIMs and verify actual page
    # text. The candidate list is deliberately bounded for serverless latency.
    for consolidated in _global_consolidated_items(graph, entry.shipment):
        name = str(consolidated.get("name", "DIM CON LEVANTE.pdf"))
        parent = _item_parent_path(consolidated)
        try:
            extracted = _extract_declaration(graph.download_item(consolidated), entry.form_number)
        except RuntimeError as exc:
            logger.info("DIM: se omite consolidado candidato (%s)", type(exc).__name__)
            continue
        diagnostics.append("Declaración extraída del DIM consolidado mediante búsqueda global")
        return LocatedPdf(extracted, f"{entry.form_number.lstrip('_')}.pdf", parent, _item_drive_base(graph, consolidated), True)

    # Last resort: Graph may index the form number from PDF contents even if a
    # consolidated file was renamed. Try only a few likely candidates so a miss
    # cannot turn into a long scan and a Wix-side timeout.
    for candidate in _global_pdf_content_candidates(graph, entry.form_number):
        name = str(candidate.get("name", "documento.pdf"))
        try:
            extracted = _extract_declaration(graph.download_item(candidate), entry.form_number)
        except RuntimeError as exc:
            logger.info("DIM: búsqueda por contenido descarta un PDF candidato (%s)", type(exc).__name__)
            continue
        diagnostics.append("Declaración localizada por contenido del PDF en búsqueda global")
        return LocatedPdf(
            extracted, f"{entry.form_number.lstrip('_')}.pdf", _item_parent_path(candidate),
            _item_drive_base(graph, candidate), True,
        )
    raise RuntimeError("No se encontró el contenedor o la declaración indicada en SharePoint")


def process_hierarchical_references(references: list[str], supplied_terms: list[str], settings: Settings) -> dict[str, list[dict[str, Any]]]:
    if not references or any(not isinstance(value, str) or not value.strip() for value in references):
        raise ValueError("references must be a non-empty array of strings")
    if not isinstance(supplied_terms, list) or not all(isinstance(value, str) for value in supplied_terms):
        raise ValueError("datosSensibles must be an array of strings")
    graph = GraphClient(settings)
    logger.info("DIM: descargando Excel maestro desde %s", settings.excel_path)
    index = _read_master_index(graph.download_excel(), settings)
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
