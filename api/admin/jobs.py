from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from app.http_api import ApiHandler, AuthenticationError
from app.jobs import enqueue_job, get_job, list_failed_jobs, retry_failed_items

logger = logging.getLogger(__name__)


class handler(ApiHandler):
    def do_GET(self) -> None:
        try:
            self.claims(admin=True)
            raw_limit = parse_qs(urlparse(self.path).query).get("limit", ["50"])[0]
            try:
                limit = int(raw_limit)
            except ValueError as exc:
                raise ValueError("limit debe ser un número entero") from exc
            self.respond(200, {"success": True, "jobs": list_failed_jobs(limit)})
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Se requiere acceso de administrador."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not list failed document jobs")
            self.respond(503, {"success": False, "message": "No fue posible consultar los trabajos fallidos."})

    def do_POST(self) -> None:
        try:
            claims = self.claims(admin=True)
            owner_email = str(claims.get("sub", "")).strip().lower()
            job_id = str(self.json_body().get("jobId", "")).strip()
            if not job_id:
                raise ValueError("jobId es obligatorio")
            job = get_job(job_id, owner_email, is_admin=True)
            if not job:
                self.respond(404, {"success": False, "message": "No se encontró el trabajo."})
                return
            retried = retry_failed_items(job_id)
            if not retried:
                self.respond(409, {"success": False, "message": "El trabajo no tiene elementos fallidos para reintentar."})
                return
            enqueue_job(job_id)
            job = get_job(job_id, owner_email, is_admin=True)
            self.respond(202, {
                "success": True,
                "retried": retried,
                "job": job,
                "message": "Se reprogramaron los elementos fallidos.",
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Se requiere acceso de administrador."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not retry failed document job")
            self.respond(503, {"success": False, "message": "No fue posible reintentar el trabajo."})
