from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler, AuthenticationError
from app.jobs import enqueue_job, get_download_target, get_job

logger = logging.getLogger(__name__)


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
            enqueue_job(job_id)
            job = get_job(job_id, owner_email, is_admin=is_admin)
            if job and job["status"] in {"completed", "partial"}:
                graph = None
                for item_index, result in enumerate(job["resultados"]):
                    if not result.get("downloadUrl"):
                        continue
                    target = get_download_target(job_id, item_index, 0, owner_email, is_admin)
                    if not target:
                        result["downloadUrl"] = None
                        continue
                    if graph is None:
                        graph = GraphClient(Settings.from_env())
                    # The temporary Graph link is generated only in this
                    # authenticated response and is never written into Neon.
                    result["downloadUrl"] = graph.fresh_download_url(
                        target["driveItemId"], target["driveBase"]
                    )
            self.respond(200, {"success": True, **job})
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not read DIM job status")
            self.respond(503, {"success": False, "message": "No fue posible consultar el estado. Inténtalo de nuevo."})
