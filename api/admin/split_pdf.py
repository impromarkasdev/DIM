from __future__ import annotations

import logging
import re

from app.http_api import ApiHandler, AuthenticationError
from app.jobs import create_job, enqueue_job, get_job

logger = logging.getLogger(__name__)
MAX_FILE_PATH_LENGTH = 4096


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            claims = self.claims(admin=True)
            owner_email = str(claims.get("sub", "")).strip().lower()
            if not owner_email or "@" not in owner_email:
                self.respond(401, {"success": False, "message": "La sesión no contiene un usuario válido."})
                return

            file_path = str(self.json_body().get("filePath", "")).strip()
            if (
                not file_path
                or len(file_path) > MAX_FILE_PATH_LENGTH
                or not (file_path.casefold().endswith(".pdf") or file_path.startswith(("id:", "https://")))
                or (file_path.startswith("https://") and not re.match(r"^https://[^/]+/.+\.pdf(?:\?.*)?$", file_path, re.IGNORECASE))
            ):
                raise ValueError("filePath debe ser la ruta o URL HTTPS de un PDF, o un valor id:<DriveItem-id>")

            idempotency_key = self.headers.get("Idempotency-Key", "").strip() or None
            if idempotency_key and (len(idempotency_key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key)):
                raise ValueError("Idempotency-Key no tiene un formato válido")

            job_id, created = create_job(
                owner_email,
                ["__split_pdf__"],
                [],
                idempotency_key=idempotency_key,
                job_type="split_pdf",
                job_metadata={"file_path": file_path},
            )
            enqueue_job(job_id)
            job = get_job(job_id, owner_email, is_admin=True)
            if not job:
                raise RuntimeError("No fue posible recuperar el trabajo recién creado")
            self.respond(202, {
                "success": True,
                "jobId": job_id,
                "jobType": "split_pdf",
                "status": job["status"],
                "total": 1,
                "completed": 0,
                "statusUrl": f"/api/documents/jobs?jobId={job_id}",
                "created": created,
                "message": "El PDF se recibió y se separará en segundo plano.",
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Se requiere acceso de administrador."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not submit PDF split job")
            self.respond(503, {
                "success": False,
                "message": "No fue posible poner la separación en la cola. Inténtalo nuevamente.",
            })
