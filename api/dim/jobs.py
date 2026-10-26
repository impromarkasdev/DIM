from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler, AuthenticationError
from app.jobs import enqueue_job, get_download_target, get_job

logger = logging.getLogger(__name__)


def _refresh_download_links(job: dict, job_id: str, owner_email: str, is_admin: bool) -> None:
    """Best-effort refresh of ephemeral links; never changes durable job state."""
    graph = None
    for item_index, result in enumerate(job["resultados"]):
        if not result.get("downloadUrl"):
            continue
        target = get_download_target(job_id, item_index, 0, owner_email, is_admin)
        if not target:
            result["downloadUrl"] = None
            continue
        try:
            if graph is None:
                graph = GraphClient(Settings.from_env())
            # Temporary Graph links are emitted only in this authenticated
            # response and are never persisted in Neon.
            result["downloadUrl"] = graph.fresh_download_url(
                target["driveItemId"], target["driveBase"]
            )
            result.pop("downloadLinkPending", None)
            result.pop("downloadMessage", None)
        except Exception as exc:
            # Keep status polling independent from transient Graph link errors.
            logger.warning(
                "Could not mint download link for completed job %s item %s (%s)",
                job_id, item_index, type(exc).__name__,
            )
            result["downloadUrl"] = None
            result["downloadLinkPending"] = True
            result["downloadMessage"] = (
                "El documento ya está procesado; no se pudo preparar el enlace de descarga. "
                "Consulta nuevamente en unos momentos."
            )


class handler(ApiHandler):
    def do_GET(self) -> None:
        try:
            claims = self.claims()
            owner_email = str(claims.get("sub", "")).strip().lower()
            if not owner_email:
                self.respond(401, {"success": False, "message": "La sesión no contiene un usuario válido."})
                return
            job_id = parse_qs(urlparse(self.path).query).get("jobId", [""])[0].strip()
            if not job_id:
                self.respond(400, {"success": False, "message": "Falta el parámetro jobId."})
                return
            is_admin = claims.get("role") == "admin"
            job = get_job(job_id, owner_email, is_admin=is_admin)
            if not job:
                self.respond(404, {"success": False, "message": "No se encontró el trabajo solicitado."})
                return
            # Database state is the durable outbox. If the initial publisher
            # was interrupted, polling the job retries unsent queue messages.
            queue_dispatch_pending = False
            try:
                queue_dispatch_pending = enqueue_job(job_id) > 0
            except Exception:
                # Status reads must remain available even when a queue
                # publication or its bookkeeping hits a transient DB error.
                queue_dispatch_pending = True
                logger.exception("Queue redispatch failed while reading DIM job %s", job_id)
            job = get_job(job_id, owner_email, is_admin=is_admin)
            if job and job["status"] in {"completed", "partial"} and job.get("jobType") != "invoice_redact":
                _refresh_download_links(job, job_id, owner_email, is_admin)
            self.respond(200, {
                "success": True,
                **job,
                "queueDispatchPending": queue_dispatch_pending,
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not read DIM job status")
            self.respond(503, {"success": False, "message": "No fue posible consultar el estado. Inténtalo de nuevo."})
