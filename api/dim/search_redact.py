from __future__ import annotations

import logging
import re

from app.http_api import ApiHandler, AuthenticationError
from app.jobs import create_job, enqueue_job, get_job

logger = logging.getLogger(__name__)
MAX_REFERENCES_PER_JOB = 30


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            claims = self.claims()
            owner_email = str(claims.get("sub", "")).strip().lower()
            if not owner_email or "@" not in owner_email:
                self.respond(401, {"success": False, "message": "La sesión no contiene un usuario válido."})
                return

            body = self.json_body()
            references = body.get("references", body.get("referencias"))
            if isinstance(references, str):
                references = [references]
            if (
                not isinstance(references, list)
                or not references
                or len(references) > MAX_REFERENCES_PER_JOB
                or not all(isinstance(value, str) and value.strip() and len(value.strip()) <= 200 for value in references)
            ):
                raise ValueError(f"references debe contener entre 1 y {MAX_REFERENCES_PER_JOB} referencias válidas")

            sensitive_terms = body.get("datosSensibles", [])
            if (
                not isinstance(sensitive_terms, list)
                or len(sensitive_terms) > 100
                or not all(isinstance(value, str) and len(value) <= 500 for value in sensitive_terms)
            ):
                raise ValueError("datosSensibles debe ser una lista válida de textos")

            normalized_references = [value.strip() for value in references]
            normalized_terms = [value.strip() for value in sensitive_terms if value.strip()]
            idempotency_key = self.headers.get("Idempotency-Key", "").strip() or None
            if idempotency_key and (len(idempotency_key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key)):
                raise ValueError("Idempotency-Key no tiene un formato válido")

            job_id, created = create_job(
                owner_email, normalized_references, normalized_terms, idempotency_key
            )
            # Re-publishing queued items is safe: every message has a stable
            # Vercel Queue idempotency key derived from the persisted job/item.
            enqueue_job(job_id)
            job = get_job(job_id, owner_email)
            if not job:
                raise RuntimeError("No fue posible recuperar el trabajo recién creado")
            self.respond(202, {
                "success": True,
                "jobId": job_id,
                "status": job["status"],
                "total": job["total"],
                "completed": job["completed"],
                "statusUrl": f"/api/documents/jobs?jobId={job_id}",
                "created": created,
                "message": "Solicitud recibida para procesamiento seguro.",
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not submit DIM redaction job")
            self.respond(503, {
                "success": False,
                "message": "No fue posible poner la solicitud en la cola. Inténtalo nuevamente.",
            })
