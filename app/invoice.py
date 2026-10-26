from __future__ import annotations

import io
import logging
import re
from collections import OrderedDict
from typing import Any

from app.config import Settings
from app.graph import GraphClient
from app.service import DimIndexRow, _plain, _read_master_index

logger = logging.getLogger(__name__)
MAX_INVOICE_REFERENCES = 1000


def _invoice_key(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return "".join(char for char in text.casefold() if char.isalnum())


def _declaration_key(value: str) -> str:
    digits = "".join(re.findall(r"\d", str(value)))
    if len(digits) in {15, 16}:
        return digits[:15]
    return _plain(str(value))


def _cell_value(row: tuple[Any, ...], index: int) -> Any:
    return row[index].v if index < len(row) and row[index] is not None else None


def _read_sales_references(workbook_bytes: bytes, invoice_number: str) -> list[str]:
    """Scan only configured columns C (invoice) and K (reference) in XLSB."""
    try:
        from pyxlsb import open_workbook

        target = _invoice_key(invoice_number)
        if not target:
            raise ValueError("Ingresa un número de factura válido.")
        references: OrderedDict[str, str] = OrderedDict()
        with open_workbook(io.BytesIO(workbook_bytes)) as workbook:
            sheet_name = "BD" if "BD" in workbook.sheets else workbook.sheets[0]
            with workbook.get_sheet(sheet_name) as sheet:
                for row_number, row in enumerate(sheet.rows(sparse=True), start=1):
                    invoice = _cell_value(row, 2)
                    reference = _cell_value(row, 10)
                    if row_number == 1 and str(invoice or "").strip().casefold() in {"factura", "invoice"}:
                        continue
                    if _invoice_key(invoice) != target:
                        continue
                    reference_text = str(reference or "").strip()
                    if reference_text.endswith(".0") and reference_text[:-2].isdigit():
                        reference_text = reference_text[:-2]
                    key = _plain(reference_text)
                    if key:
                        references.setdefault(key, reference_text)
        logger.info("Invoice workbook scan finished: matched %s distinct references", len(references))
        if len(references) > MAX_INVOICE_REFERENCES:
            raise ValueError(f"La factura supera el máximo operativo de {MAX_INVOICE_REFERENCES} referencias únicas.")
        return list(references.values())
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Could not read the sales XLSB workbook")
        raise RuntimeError("No fue posible leer la base de ventas XLSB.") from exc


def resolve_invoice_items(
    invoice_number: str, supplied_terms: list[str], settings: Settings
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve invoice SKU references against the latest single DIM per reference.

    Duplicate references are collapsed; references pointing at the same DIM
    form become one work item with all associated references attached.
    """
    graph = GraphClient(settings)
    logger.info("Invoice lookup: downloading current sales workbook from configured SharePoint URL")
    sales_bytes, sales_name, _, _ = graph.resolve_file(settings.sales_excel_path)
    if not sales_name.casefold().endswith(".xlsb"):
        raise ValueError("SALES_EXCEL_PATH no apunta a un archivo XLSB.")
    references = _read_sales_references(sales_bytes, invoice_number)
    if not references:
        return ({
            "factura": invoice_number,
            "referenciasFactura": 0,
            "declaracionesUnicas": 0,
            "referenciasSinDIM": 0,
            "estado": "Factura no encontrada o sin referencias",
        }, [])

    logger.info("Invoice lookup: loading DIM master index once for %s references", len(references))
    # Invoice lookups use a fresh DIM workbook snapshot. The sales XLSB is also
    # downloaded once per job; neither workbook is fetched per child declaration.
    index = _read_master_index(graph.download_excel(), settings)
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    unmatched: list[dict[str, Any]] = []
    for reference in references:
        entry: DimIndexRow | None = index.get(_plain(reference))
        if not entry:
            unmatched.append({
                "kind": "unmatched", "invoice": invoice_number,
                "reference": reference, "references": [reference],
            })
            continue
        form_key = _declaration_key(entry.form_number)
        if not form_key:
            unmatched.append({
                "kind": "unmatched", "invoice": invoice_number,
                "reference": reference, "references": [reference],
            })
            continue
        current = grouped.get(form_key)
        if current is None:
            grouped[form_key] = {
                "kind": "declaration", "invoice": invoice_number,
                "reference": reference, "references": [reference],
                "year": entry.year, "shipment": entry.shipment,
                "form_number": entry.form_number, "accepted_date": entry.accepted_date,
            }
        elif reference not in current["references"]:
            current["references"].append(reference)

    declaration_items = list(grouped.values())
    items = declaration_items + unmatched
    summary = {
        "factura": invoice_number,
        "referenciasFactura": len(references),
        "declaracionesUnicas": len(declaration_items),
        "referenciasSinDIM": len(unmatched),
        "estado": "Referencias resueltas",
    }
    logger.info(
        "Invoice lookup resolved: invoice references=%s unique DIMs=%s unmatched=%s",
        len(references), len(declaration_items), len(unmatched),
    )
    return summary, items


def entry_from_metadata(metadata: dict[str, Any]) -> DimIndexRow:
    return DimIndexRow(
        reference=str(metadata.get("reference", "")),
        year=str(metadata.get("year", "")),
        shipment=str(metadata.get("shipment", "")),
        form_number=str(metadata.get("form_number", "")),
        accepted_date=str(metadata.get("accepted_date", "")),
    )
