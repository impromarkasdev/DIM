from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler, AuthenticationError
from app.jobs import get_download_target

logger = logging.getLogger(__name__)


class handler(ApiHandler):
    def do_GET(self) -> None:
        try:
            claims = self.claims()
            owner_email = str(claims.get("sub", "")).strip().lower()
            is_admin = claims.get("role") == "admin"
            query = parse_qs(urlparse(self.path).query)
            job_id = query.get("jobId", [""])[0].strip()
            try:
                item_index = int(query.get("itemIndex", ["0"])[0])
                file_index = int(query.get("fileIndex", ["0"])[0])
            except ValueError as exc:
                raise ValueError("Los índices de descarga no son válidos") from exc
            if not job_id or item_index < 0 or file_index < 0:
                raise ValueError("Faltan los datos de descarga")

            target = get_download_target(job_id, item_index, file_index, owner_email, is_admin)
            if not target:
                self.respond(404, {"success": False, "message": "El archivo no está disponible para esta cuenta."})
                return

            content = GraphClient(Settings.from_env()).download_item(
                {"id": target["driveItemId"]}, drive_base=target["driveBase"]
            )
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", target["name"])
            self.binary(
                200,
                content,
                "application/pdf",
                filename,
                {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
            )
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "No tienes permiso para descargar este documento."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            logger.exception("Authenticated document download failed")
            self.respond(502, {"success": False, "message": "No fue posible descargar el archivo. Inténtalo de nuevo."})
