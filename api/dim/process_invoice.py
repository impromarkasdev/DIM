from __future__ import annotations

import logging
import re

from app.http_api import ApiHandler, AuthenticationError
from app.jobs import create_job, enqueue_job, get_job

logger = logging.getLogger(__name__)
MAX_INVOICE_LENGTH = 120


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            claims = self.claims()
            owner_email = str(claims.get("sub", "")).strip().lower()
            if not owner_email or "@" not in owner_email:
                self.respond(401, {"success": False, "message": "La sesión no contiene un usuario válido."})
                return
            body = self.json_body()
            invoice = str(body.get("invoice", body.get("factura", ""))).strip()
            if not invoice or len(invoice) > MAX_INVOICE_LENGTH or not re.fullmatch(r"[A-Za-z0-9 ./_-]+", invoice):
                raise ValueError("invoice debe ser un número de factura válido.")
            sensitive_terms = body.get("datosSensibles", [])
            if not isinstance(sensitive_terms, list) or len(sensitive_terms) > 100 or not all(
                isinstance(value, str) and len(value) <= 500 for value in sensitive_terms
            ):
                raise ValueError("datosSensibles debe ser una lista válida de textos")
            terms = [value.strip() for value in sensitive_terms if value.strip()]
            idempotency_key = self.headers.get("Idempotency-Key", "").strip() or None
            if idempotency_key and (
                len(idempotency_key) > 128
                or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key)
            ):
                raise ValueError("Idempotency-Key no tiene un formato válido")

            job_id, created = create_job(
                owner_email,
                [invoice],
                terms,
                idempotency_key=idempotency_key,
                job_type="invoice_redact",
                job_metadata={"invoice": invoice},
            )
            queue_pending = False
            try:
                queue_pending = enqueue_job(job_id) > 0
            except Exception:
                queue_pending = True
                logger.exception("Initial queue dispatch failed for invoice job %s", job_id)
            job = get_job(job_id, owner_email)
            if not job:
                raise RuntimeError("No fue posible recuperar el trabajo recién creado")
            self.respond(202, {
                "success": True,
                "jobId": job_id,
                "jobType": "invoice_redact",
                "status": job["status"],
                "total": job["total"],
                "completed": job["completed"],
                "statusUrl": f"/api/documents/jobs?jobId={job_id}",
                "created": created,
                "queueDispatchPending": queue_pending,
                "message": "La factura quedó en cola para cruzar sus referencias y preparar las declaraciones.",
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not submit DIM invoice job")
            self.respond(503, {"success": False, "message": "No fue posible poner la factura en la cola. Inténtalo nuevamente."})
