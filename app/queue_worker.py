from __future__ import annotations

import logging
import os
import time
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests
from vercel.queue import Message, RetryAfter, subscribe

from .config import Settings
from .errors import PermanentProcessingError
from .graph import GraphClient
from .jobs import (
    _safe_error_message, claim_item, complete_item, enqueue_job,
    expand_invoice_job, list_job_download_targets, retry_or_fail_item,
)
from .pdf_splitter import split_declarations_with_report
from .service import process_dim_entry, process_hierarchical_references
from .invoice import entry_from_metadata, resolve_invoice_items

logger = logging.getLogger(__name__)
MAX_DELIVERIES = 10


def _alert_terminal_failure(job_id: str, item_index: int, error: str, attempt_count: int) -> None:
    """Send a minimal, secret-free alert to an optional admin webhook."""
    logger.critical(
        "Document job permanently failed: job=%s item=%s error=%s",
        job_id, item_index, error[:300],
    )
    webhook = os.getenv("ADMIN_ALERT_WEBHOOK_URL", "").strip()
    parsed = urlparse(webhook)
    if not webhook:
        logger.critical("ADMIN_ALERT_WEBHOOK_URL is not configured; failure is visible in Vercel logs/admin jobs")
        return
    if parsed.scheme != "https" or not parsed.netloc:
        logger.error("Admin alert webhook is not a valid HTTPS URL")
        return
    try:
        response = requests.post(
            webhook,
            json={
                "text": f"DIM job {job_id} item {item_index} failed on delivery {attempt_count}: {error[:300]}",
                "event": "dim_job_failed",
                "jobId": job_id,
                "itemIndex": item_index,
            "attemptCount": attempt_count,
                "error": error[:300],
            },
            timeout=(3, 5),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Admin failure alert could not be delivered (%s)", type(exc).__name__)


@subscribe(topic="dim-redaction", retry_after=60, max_concurrency=10)
async def process_dim_reference(message: Message[dict[str, Any]]) -> None:
    """Queue consumer: process a single reference so each item can retry alone."""
    payload = message.payload
    job_id = str(payload.get("job_id", ""))
    try:
        item_index = int(payload.get("item_index"))
    except (TypeError, ValueError):
        logger.error("Discarding malformed DIM queue message")
        return
    if not job_id:
        logger.error("Discarding DIM queue message without job_id")
        return

    claimed = claim_item(job_id, item_index)
    if not claimed:
        logger.warning("DIM queue message has no matching item: job=%s index=%s", job_id, item_index)
        return
    if claimed.get("busy"):
        # A duplicate delivery may arrive while the previous invocation still
        # owns the DB lease. Keep this queue message alive until that lease ends.
        raise RetryAfter(int(claimed.get("retry_after", 60)))

    delivery_count = int(message.metadata.delivery_count)
    reference = claimed["reference"]
    started_at = time.monotonic()
    try:
        settings = Settings.from_env()
        if claimed["job_type"] == "split_pdf":
            file_path = str(claimed.get("file_path") or "").strip()
            graph = GraphClient(settings)
            logger.info("Split job %s: downloading PDF source", job_id)
            content, filename, source_drive, parent_folder = graph.resolve_file(file_path)
            files, skipped_pages = split_declarations_with_report(content)
            output_folder = f"{parent_folder}/Procesados" if parent_folder else "Procesados"
            logger.info("Split job %s: uploading %s declarations", job_id, len(files))
            uploaded = graph.upload_many_pdfs(output_folder, files, drive_base=source_drive)
            download_files = []
            for (name, _), metadata in zip(files, uploaded):
                if not metadata.get("id"):
                    raise RuntimeError("Microsoft Graph guardó un PDF sin devolver su identificador")
                drive_id = metadata.get("parentReference", {}).get("driveId")
                stable_drive_base = f"{graph.base}/drives/{drive_id}" if drive_id else source_drive
                download_files.append({
                    "driveItemId": str(metadata["id"]),
                    "driveBase": stable_drive_base,
                    "name": name,
                })
            result = {
                "estado": "Exito",
                "message": "PDF dividido con éxito",
                "count": len(files),
                "outputFolder": f"/{output_folder}",
                "files": [name for name, _ in files],
                "downloadFiles": download_files,
                "skippedPages": skipped_pages,
            }
        elif claimed["job_type"] == "invoice_redact" and item_index == 0:
            invoice = str((claimed.get("payload") or {}).get("invoice", reference)).strip()
            summary, declaration_items = resolve_invoice_items(
                invoice, claimed.get("sensitive_terms", []), settings
            )
            parent_result = {
                **summary,
                "estado": "Exito",
                "factura": invoice,
                "referencia": f"Factura {invoice}",
            }
            expanded = expand_invoice_job(job_id, parent_result, declaration_items)
            logger.info(
                "Invoice job expanded: job=%s invoice=%s declarations_and_unmatched=%s",
                job_id, invoice, expanded,
            )
            # Queue publication is outside the expansion transaction. If the
            # publisher is briefly unavailable, status polling will redispatch
            # from the durable item table; don't reopen the completed parent.
            try:
                await asyncio.to_thread(enqueue_job, job_id)
            except Exception:
                logger.exception("Could not publish expanded invoice items; status polling will retry: job=%s", job_id)
            logger.info(
                "Invoice resolution completed: job=%s invoice=%s refs=%s declarations=%s unmatched=%s",
                job_id, invoice, summary["referenciasFactura"],
                summary["declaracionesUnicas"], summary["referenciasSinDIM"],
            )
            return
        elif claimed["job_type"] == "invoice_redact":
            metadata = claimed.get("metadata") or {}
            invoice = str(metadata.get("invoice", ""))
            references_for_dim = metadata.get("references", [])
            if metadata.get("kind") == "finalize":
                handles = list_job_download_targets(job_id)
                if not handles:
                    result = {
                        "estado": "Pendiente (No encontrado)",
                        "factura": str((claimed.get("payload") or {}).get("invoice", "")),
                        "referencia": "PDF combinado",
                        "detalle": "La factura no produjo declaraciones disponibles para combinar.",
                    }
                else:
                    logger.info("Invoice finalizer: downloading %s unique redacted PDFs", len(handles))
                    combine_graph = GraphClient(settings)
                    with ThreadPoolExecutor(max_workers=min(8, len(handles))) as executor:
                        contents = list(executor.map(
                            lambda target: combine_graph.download_item(
                                {"id": target["driveItemId"]}, drive_base=target["driveBase"]
                            ),
                            handles,
                        ))
                    from api.documents.combine import _merge_pdf_contents
                    try:
                        combined = _merge_pdf_contents(contents)
                    except ValueError as exc:
                        raise PermanentProcessingError(str(exc)) from exc
                    invoice = str((claimed.get("payload") or {}).get("invoice", "")).strip()
                    safe_invoice = re.sub(r"[^A-Za-z0-9_-]+", "_", invoice)[:60] or "consulta"
                    filename = f"Factura_{safe_invoice}_{job_id[:8]}.pdf"
                    uploaded = combine_graph.upload_pdf(
                        f"DIM/ConsultasCombinadas/{filename}", combined
                    )
                    if not uploaded.get("id"):
                        raise RuntimeError("Microsoft Graph guardó el PDF combinado sin identificador")
                    drive_id = uploaded.get("parentReference", {}).get("driveId")
                    output_drive = f"{combine_graph.base}/drives/{drive_id}" if drive_id else combine_graph.drive_base
                    result = {
                        "estado": "Exito",
                        "factura": invoice,
                        "referencia": "PDF combinado",
                        "nombre_pdf": filename,
                        "numeroDeclaraciones": len(handles),
                        "downloadFiles": [{
                            "driveItemId": str(uploaded["id"]),
                            "driveBase": output_drive,
                            "name": filename,
                        }],
                    }
            elif metadata.get("kind") != "declaration":
                finalizer_created = complete_item(job_id, item_index, {
                    "estado": "Pendiente (No encontrado)",
                    "factura": invoice,
                    "referencia": reference,
                    "referenciasFactura": references_for_dim,
                    "detalle": "La referencia de esta factura no aparece en las hojas del archivo maestro DIM.",
                })
                if finalizer_created:
                    try:
                        await asyncio.to_thread(enqueue_job, job_id)
                    except Exception:
                        logger.exception("Could not publish invoice finalizer; status polling will retry: job=%s", job_id)
                logger.info("Invoice reference has no DIM match: job=%s item=%s", job_id, item_index)
                return
            entry = entry_from_metadata(metadata)
            result = process_dim_entry(
                reference,
                entry,
                claimed.get("sensitive_terms", []),
                settings,
                invoice=invoice,
                invoice_references=references_for_dim,
            )
            result["factura"] = invoice
            result["referenciasFactura"] = references_for_dim
            if result.get("estado") == "Error":
                detail = str(result.get("detalle", "No fue posible procesar el documento"))
                if result.get("_permanentError"):
                    raise PermanentProcessingError(detail)
                raise RuntimeError(detail)
        else:
            result = process_hierarchical_references(
                [reference], claimed.get("sensitive_terms", []), settings
            )["resultados"][0]
            if result.get("estado") == "Error":
                detail = str(result.get("detalle", "No fue posible procesar el documento"))
                if result.get("_permanentError"):
                    raise PermanentProcessingError(detail)
                raise RuntimeError(detail)
        finalizer_created = complete_item(job_id, item_index, result)
        if finalizer_created:
            try:
                await asyncio.to_thread(enqueue_job, job_id)
            except Exception:
                logger.exception("Could not publish invoice finalizer; status polling will retry: job=%s", job_id)
        logger.info(
            "DIM queue item completed: job=%s index=%s state=%s duration_seconds=%.2f",
            job_id, item_index, result.get("estado"), time.monotonic() - started_at,
        )
    except RetryAfter:
        raise
    except Exception as exc:
        terminal = isinstance(exc, PermanentProcessingError) or delivery_count >= MAX_DELIVERIES
        finalizer_created = retry_or_fail_item(job_id, item_index, str(exc), terminal=terminal)
        if finalizer_created:
            try:
                await asyncio.to_thread(enqueue_job, job_id)
            except Exception:
                logger.exception("Could not publish invoice finalizer after item failure: job=%s", job_id)
        if terminal:
            _alert_terminal_failure(
                job_id, item_index, _safe_error_message(str(exc)), delivery_count
            )
            return
        retry_delay = min(300, 15 * (2 ** max(0, delivery_count - 1)))
        logger.warning(
            "DIM queue item failed; retry %s/%s in %ss: job=%s index=%s duration_seconds=%.2f (%s)",
            delivery_count + 1, MAX_DELIVERIES, retry_delay, job_id, item_index,
            time.monotonic() - started_at, type(exc).__name__,
        )
        raise RetryAfter(retry_delay) from exc
