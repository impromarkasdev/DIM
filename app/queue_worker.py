from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests
from vercel.queue import Message, RetryAfter, subscribe

from .config import Settings
from .graph import GraphClient
from .jobs import _safe_error_message, claim_item, complete_item, retry_or_fail_item
from .pdf_splitter import split_declarations_with_report
from .service import process_hierarchical_references

logger = logging.getLogger(__name__)
MAX_DELIVERIES = 10


def _alert_terminal_failure(job_id: str, item_index: int, error: str) -> None:
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
                "text": f"DIM job {job_id} item {item_index} failed after {MAX_DELIVERIES} attempts: {error[:300]}",
                "event": "dim_job_failed",
                "jobId": job_id,
                "itemIndex": item_index,
                "error": error[:300],
            },
            timeout=(3, 5),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Admin failure alert could not be delivered (%s)", type(exc).__name__)


@subscribe(topic="dim-redaction", retry_after=60, max_concurrency=3)
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
        else:
            result = process_hierarchical_references(
                [reference], claimed.get("sensitive_terms", []), settings
            )["resultados"][0]
            if result.get("estado") == "Error":
                detail = str(result.get("detalle", "No fue posible procesar el documento"))
                raise RuntimeError(detail)
        complete_item(job_id, item_index, result)
        logger.info(
            "DIM queue item completed: job=%s index=%s state=%s duration_seconds=%.2f",
            job_id, item_index, result.get("estado"), time.monotonic() - started_at,
        )
    except RetryAfter:
        raise
    except Exception as exc:
        terminal = delivery_count >= MAX_DELIVERIES
        retry_or_fail_item(job_id, item_index, str(exc), terminal=terminal)
        if terminal:
            _alert_terminal_failure(job_id, item_index, _safe_error_message(str(exc)))
            return
        retry_delay = min(300, 15 * (2 ** max(0, delivery_count - 1)))
        logger.warning(
            "DIM queue item failed; retry %s/%s in %ss: job=%s index=%s duration_seconds=%.2f (%s)",
            delivery_count + 1, MAX_DELIVERIES, retry_delay, job_id, item_index,
            time.monotonic() - started_at, type(exc).__name__,
        )
        raise RetryAfter(retry_delay) from exc
