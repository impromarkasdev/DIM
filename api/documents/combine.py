from __future__ import annotations

import logging
import re
from urllib.parse import quote

import fitz

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler, AuthenticationError
from app.jobs import get_download_target, get_job

logger = logging.getLogger(__name__)
MAX_COMBINED_INPUT_BYTES = 100 * 1024 * 1024
OUTPUT_FOLDER = "DIM/ConsultasCombinadas"


def _merge_pdf_contents(contents: list[bytes]) -> bytes:
    if not contents:
        raise ValueError("No hay documentos exitosos para combinar.")
    total_bytes = sum(len(content) for content in contents)
    if total_bytes > MAX_COMBINED_INPUT_BYTES:
        raise ValueError("Los documentos superan el tamaño máximo para combinar en una sola descarga.")

    merged = fitz.open()
    try:
        for content in contents:
            with fitz.open(stream=content, filetype="pdf") as source:
                merged.insert_pdf(source)
        if merged.page_count == 0:
            raise ValueError("Los archivos no contienen páginas PDF válidas.")
        return merged.tobytes(garbage=4, deflate=True)
    finally:
        merged.close()


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            claims = self.claims()
            owner_email = str(claims.get("sub", "")).strip().lower()
            is_admin = str(claims.get("role", "")).strip().lower() == "admin"
            body = self.json_body()
            job_id = str(body.get("jobId", "")).strip()
            if not job_id:
                raise ValueError("Falta el identificador de la solicitud.")

            job = get_job(job_id, owner_email, is_admin=is_admin)
            if not job:
                self.respond(404, {"success": False, "message": "No se encontró la solicitud."})
                return
            if job.get("jobType") != "redact":
                self.respond(400, {"success": False, "message": "Solo se pueden combinar resultados de búsqueda y redacción."})
                return
            if str(job.get("status", "")).lower() not in {"completed", "partial"}:
                self.respond(409, {
                    "success": False,
                    "message": "La consulta todavía no termina.",
                    "completed": job.get("completed", 0),
                    "total": job.get("total", 0),
                })
                return

            handles: list[dict[str, str]] = []
            for item_index, item in enumerate(job.get("resultados", [])):
                if item.get("estado") != "Exito":
                    continue
                target = get_download_target(job_id, item_index, 0, owner_email, is_admin)
                if target:
                    handles.append(target)
            if not handles:
                self.respond(404, {"success": False, "message": "No hay documentos encontrados para combinar."})
                return

            settings = Settings.from_env()
            graph = GraphClient(settings)
            filename = f"Declaraciones_{re.sub(r'[^A-Za-z0-9-]', '', job_id)[:12]}.pdf"
            output_path = f"{OUTPUT_FOLDER}/{filename}"
            pdf_contents = [
                graph.download_item({"id": handle["driveItemId"]}, drive_base=handle["driveBase"])
                for handle in handles
            ]
            combined = _merge_pdf_contents(pdf_contents)
            uploaded = graph.upload_pdf(output_path, combined)
            drive_id = uploaded.get("parentReference", {}).get("driveId")
            output_drive = f"{graph.base}/drives/{quote(str(drive_id), safe='')}" if drive_id else graph.drive_base
            download_url = graph.fresh_download_url(str(uploaded["id"]), output_drive)

            self.respond(200, {
                "success": True,
                "downloadUrl": download_url,
                "fileName": filename,
                "combinedCount": len(handles),
                "message": "Los documentos disponibles se combinaron en un solo PDF.",
            })
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "No tienes permiso para combinar esta solicitud."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Could not combine redacted DIM documents")
            self.respond(502, {"success": False, "message": "No fue posible preparar el PDF combinado. Inténtalo nuevamente."})
