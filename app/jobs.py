from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .users import _connection_pool, ensure_jobs_schema

logger = logging.getLogger(__name__)
QUEUE_TOPIC = "dim-redaction"


def _safe_error_message(message: str) -> str:
    """Keep useful diagnostics while stripping links and credential-like values."""
    safe = re.sub(r"https?://\S+", "[enlace omitido]", str(message))
    safe = re.sub(
        r"(?i)\b(authorization|access_token|refresh_token|client_secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[oculto]",
        safe,
    )
    return safe[:400]


def _queue_client() -> Any:
    # Imported lazily so login and other APIs do not depend on queue setup.
    from vercel.queue import QueueClient

    return QueueClient()


def create_job(
    owner_email: str,
    references: list[str],
    sensitive_terms: list[str],
    idempotency_key: str | None = None,
    job_type: str = "redact",
    job_metadata: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Persist a job and its per-reference work items before queue publishing."""
    ensure_jobs_schema()
    if job_type not in {"redact", "split_pdf"}:
        raise ValueError("Unsupported document job type")
    job_id = uuid4()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dim_jobs(id,owner_email,idempotency_key,job_type,status,total,payload)
            VALUES(%s,%s,%s,%s,'queued',%s,%s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                job_id, owner_email.lower(), idempotency_key, job_type, len(references),
                Jsonb({"sensitive_terms": sensitive_terms, **(job_metadata or {})}),
            ),
        )
        created = cursor.fetchone()
        if not created and idempotency_key:
            cursor.execute(
                "SELECT id FROM dim_jobs WHERE owner_email=%s AND idempotency_key=%s",
                (owner_email.lower(), idempotency_key),
            )
            existing = cursor.fetchone()
            if existing:
                return str(existing["id"]), False
        elif not created:
            raise RuntimeError("Could not create the document-processing job")

        cursor.executemany(
            "INSERT INTO dim_job_items(id,job_id,item_index,reference) VALUES(%s,%s,%s,%s)",
            [(uuid4(), job_id, index, reference) for index, reference in enumerate(references)],
        )
    return str(job_id), True


def _refresh_job(cursor: Any, job_id: UUID) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) FILTER (WHERE status IN ('completed','not_found','failed','enqueue_failed')) AS terminal,
               COUNT(*) FILTER (WHERE status='failed' OR status='enqueue_failed') AS failed,
               COUNT(*) FILTER (WHERE status='processing') AS processing,
               (SELECT error FROM dim_job_items WHERE job_id=%s AND error IS NOT NULL ORDER BY updated_at DESC LIMIT 1) AS last_error
        FROM dim_job_items WHERE job_id=%s
        """,
        (job_id, job_id),
    )
    counts = cursor.fetchone()
    cursor.execute(
        """
        UPDATE dim_jobs AS job
        SET completed=%s,
            failed=%s,
            status=CASE
                WHEN %s >= job.total THEN CASE WHEN %s=0 THEN 'completed' WHEN %s=job.total THEN 'failed' ELSE 'partial' END
                WHEN %s>0 OR %s>0 THEN 'processing'
                ELSE 'queued'
            END,
            payload=CASE WHEN %s >= job.total AND %s=0 THEN '{}'::jsonb ELSE job.payload END,
            last_error=%s,
            updated_at=NOW()
        WHERE id=%s
        """,
        (
            counts["terminal"], counts["failed"], counts["terminal"], counts["failed"],
            counts["failed"], counts["failed"], counts["processing"],
            counts["terminal"], counts["failed"], counts["last_error"], job_id,
        ),
    )


def enqueue_job(job_id: str) -> None:
    """Publish each reference independently; Queue delivery is at-least-once."""
    ensure_jobs_schema()
    parsed_id = UUID(job_id)
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT item_index,reference,queue_generation FROM dim_job_items
            WHERE job_id=%s AND status='queued'
              AND (queue_attempted_at IS NULL OR queue_attempted_at < NOW()-INTERVAL '5 minutes')
            ORDER BY item_index
            """,
            (parsed_id,),
        )
        pending = list(cursor.fetchall())

    if not pending:
        return
    queue = _queue_client()
    semaphore = asyncio.Semaphore(5)

    async def publish(item: dict[str, Any]) -> tuple[int, Exception | None]:
        item_index = int(item["item_index"])
        payload = {"job_id": job_id, "item_index": item_index}
        key = f"dim-{job_id}-{item_index}-{int(item['queue_generation'])}"
        async with semaphore:
            for attempt in range(3):
                try:
                    await queue.send(QUEUE_TOPIC, payload, idempotency_key=key)
                    return item_index, None
                except Exception as exc:
                    if attempt == 2:
                        return item_index, exc
                    await asyncio.sleep(0.4 * (2 ** attempt))
        return item_index, RuntimeError("Queue publish failed")

    async def publish_all() -> list[tuple[int, Exception | None]]:
        return list(await asyncio.gather(*(publish(item) for item in pending)))

    outcomes = asyncio.run(publish_all()) if pending else []
    for item_index, last_error in outcomes:
        if last_error is not None:
            logger.error(
                "Queue publish failed for job %s item %s (%s)",
                job_id, item_index, type(last_error).__name__,
            )
            _set_enqueue_attempt(parsed_id, item_index, _safe_error_message(str(last_error)))
        else:
            _set_enqueue_attempt(parsed_id, item_index, None)


def _set_enqueue_attempt(job_id: UUID, item_index: int, error: str | None) -> None:
    safe_error = _safe_error_message(error) if error else None
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE dim_job_items SET queue_attempted_at=NOW(),error=%s,updated_at=NOW() WHERE job_id=%s AND item_index=%s AND status='queued'",
            (safe_error, job_id, item_index),
        )
        cursor.execute("UPDATE dim_jobs SET last_error=%s WHERE id=%s", (safe_error, job_id))
        _refresh_job(cursor, job_id)


def claim_item(job_id: str, item_index: int) -> dict[str, Any] | None:
    """Claim a queue delivery once, with a lease to avoid duplicate execution."""
    ensure_jobs_schema()
    parsed_id = UUID(job_id)
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE dim_job_items AS item
            SET status='processing',attempts=attempts+1,lease_until=NOW()+INTERVAL '8 minutes',
                error=NULL,updated_at=NOW()
            FROM dim_jobs AS job
            WHERE item.job_id=job.id AND job.id=%s AND item.item_index=%s
              AND (item.status='queued' OR (item.status='processing' AND item.lease_until<NOW()))
            RETURNING item.reference,item.attempts,job.payload,job.job_type
            """,
            (parsed_id, item_index),
        )
        claimed = cursor.fetchone()
        if claimed:
            cursor.execute("UPDATE dim_jobs SET status='processing',updated_at=NOW() WHERE id=%s", (parsed_id,))
            return {
                "reference": str(claimed["reference"]),
                "attempts": int(claimed["attempts"]),
                "job_type": str(claimed["job_type"]),
                "sensitive_terms": (claimed["payload"] or {}).get("sensitive_terms", []),
                "file_path": (claimed["payload"] or {}).get("file_path"),
            }
        cursor.execute(
            "SELECT status,lease_until FROM dim_job_items WHERE job_id=%s AND item_index=%s",
            (parsed_id, item_index),
        )
        current = cursor.fetchone()
        if not current:
            return None
        if current["status"] == "processing" and current["lease_until"]:
            seconds = max(5, int((current["lease_until"] - datetime.now(current["lease_until"].tzinfo)).total_seconds()))
            return {"busy": True, "retry_after": min(seconds, 300)}
        return None


def complete_item(job_id: str, item_index: int, result: dict[str, Any]) -> None:
    parsed_id = UUID(job_id)
    state = "completed" if result.get("estado") == "Exito" else "not_found"
    # Graph downloadUrl values are short-lived bearer links. Never persist them;
    # persist only DriveItem identifiers and mint an authenticated app URL.
    durable_result = {key: value for key, value in result.items() if key != "downloadUrl"}
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE dim_job_items SET status=%s,result=%s,error=NULL,lease_until=NULL,updated_at=NOW() WHERE job_id=%s AND item_index=%s",
            (state, Jsonb(durable_result), parsed_id, item_index),
        )
        _refresh_job(cursor, parsed_id)


def retry_or_fail_item(job_id: str, item_index: int, error: str, terminal: bool) -> None:
    parsed_id = UUID(job_id)
    safe_error = _safe_error_message(error)
    status = "failed" if terminal else "queued"
    result = Jsonb({"estado": "Error", "detalle": safe_error}) if terminal else None
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE dim_job_items SET status=%s,error=%s,result=COALESCE(%s,result),lease_until=NULL,updated_at=NOW() WHERE job_id=%s AND item_index=%s",
            (status, safe_error, result, parsed_id, item_index),
        )
        cursor.execute("UPDATE dim_jobs SET last_error=%s WHERE id=%s", (safe_error, parsed_id))
        _refresh_job(cursor, parsed_id)


def retry_failed_items(job_id: str) -> int:
    """Reset terminal items and advance their queue idempotency generation."""
    ensure_jobs_schema()
    parsed_id = UUID(job_id)
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE dim_job_items
            SET status='queued', attempts=0, queue_generation=queue_generation+1,
                queue_attempted_at=NULL, lease_until=NULL, result=NULL, error=NULL, updated_at=NOW()
            WHERE job_id=%s AND status IN ('failed','enqueue_failed')
            """,
            (parsed_id,),
        )
        count = cursor.rowcount
        if count:
            cursor.execute("UPDATE dim_jobs SET status='queued',last_error=NULL,updated_at=NOW() WHERE id=%s", (parsed_id,))
            _refresh_job(cursor, parsed_id)
        return count


def get_download_target(
    job_id: str, item_index: int, file_index: int, owner_email: str, is_admin: bool = False
) -> dict[str, str] | None:
    ensure_jobs_schema()
    try:
        parsed_id = UUID(job_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("jobId must be a UUID") from exc
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT item.result FROM dim_job_items AS item JOIN dim_jobs AS job ON job.id=item.job_id
               WHERE job.id=%s AND item.item_index=%s AND (%s OR job.owner_email=%s)""",
            (parsed_id, item_index, is_admin, owner_email.lower()),
        )
        row = cursor.fetchone()
    if not row or not row["result"]:
        return None
    handles = row["result"].get("downloadFiles", [])
    if not isinstance(handles, list) or not 0 <= file_index < len(handles):
        return None
    handle = handles[file_index]
    if not isinstance(handle, dict):
        return None
    drive_item_id = str(handle.get("driveItemId", "")).strip()
    drive_base = str(handle.get("driveBase", "")).strip()
    filename = str(handle.get("name", "documento.pdf")).strip()
    if not drive_item_id or not drive_base or not filename.lower().endswith(".pdf"):
        return None
    return {"driveItemId": drive_item_id, "driveBase": drive_base, "name": filename}


def list_failed_jobs(limit: int = 50) -> list[dict[str, Any]]:
    ensure_jobs_schema()
    bounded_limit = max(1, min(int(limit), 100))
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,owner_email,job_type,status,total,completed,failed,last_error,updated_at
               FROM dim_jobs WHERE failed > 0 ORDER BY updated_at DESC LIMIT %s""",
            (bounded_limit,),
        )
        rows = list(cursor.fetchall())
    return [
        {
            "jobId": str(row["id"]),
            "ownerEmail": row["owner_email"],
            "jobType": row["job_type"],
            "status": row["status"],
            "total": int(row["total"]),
            "completed": int(row["completed"]),
            "failed": int(row["failed"]),
            "lastError": _safe_error_message(row["last_error"] or "Proceso finalizado con errores"),
            "updatedAt": row["updated_at"].isoformat(),
        }
        for row in rows
    ]


def get_job(job_id: str, owner_email: str, is_admin: bool = False) -> dict[str, Any] | None:
    ensure_jobs_schema()
    try:
        parsed_id = UUID(job_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("jobId must be a UUID") from exc
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,owner_email,job_type,status,total,completed,failed,created_at,updated_at,last_error FROM dim_jobs WHERE id=%s AND (%s OR owner_email=%s)",
            (parsed_id, is_admin, owner_email.lower()),
        )
        job = cursor.fetchone()
        if not job:
            return None
        cursor.execute(
            "SELECT item_index,reference,status,attempts,result,error FROM dim_job_items WHERE job_id=%s ORDER BY item_index",
            (parsed_id,),
        )
        items = list(cursor.fetchall())
    return {
        "jobId": str(job["id"]),
        "jobType": job["job_type"],
        "status": job["status"],
        "total": int(job["total"]),
        "completed": int(job["completed"]),
        "failed": int(job["failed"]),
        "createdAt": job["created_at"].isoformat(),
        "updatedAt": job["updated_at"].isoformat(),
        "lastError": _safe_error_message(job["last_error"]) if job["last_error"] else None,
        "resultados": [
            {
                "referencia": "PDF maestro" if job["job_type"] == "split_pdf" else item["reference"],
                "estado": item["result"].get("estado") if item["result"] else item["status"],
                "attempts": int(item["attempts"]),
                "nombre_pdf": item["result"].get("nombre_pdf") if item["result"] else None,
                "downloadUrl": (
                    f"/api/documents/download?jobId={job['id']}&itemIndex={item['item_index']}&fileIndex=0"
                    if item["result"] and item["result"].get("downloadFiles") else None
                ),
                "count": item["result"].get("count") if item["result"] else None,
                "outputFolder": item["result"].get("outputFolder") if item["result"] else None,
                "files": item["result"].get("files") if item["result"] else None,
                "downloadUrls": None,
                "skippedPages": item["result"].get("skippedPages") if item["result"] else None,
                "message": item["result"].get("message") if item["result"] else None,
                "detalle": (
                    _safe_error_message(item["result"].get("detalle", item["error"]))
                    if item["result"] and item["result"].get("detalle", item["error"])
                    else _safe_error_message(item["error"]) if item["error"] else None
                ),
                "diagnostico": item["result"].get("diagnostico", []) if item["result"] else [],
            }
            for item in items
        ],
    }
