from __future__ import annotations

import base64
import traceback

from app.downloads import zip_files
from app.http_api import ApiHandler
from app.config import Settings
from app.service import process_references


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims()
            result = process_references(self.json_body(), Settings.from_env())
            successful = [item for item in result["resultados"] if item.get("estado") == "Exito"]
            if not successful:
                self.respond(404, result)
                return
            files = [(item["nombre_pdf"], base64.b64decode(item.pop("pdf_base64"))) for item in successful]
            if len(files) == 1:
                self.binary(200, files[0][1], "application/pdf", files[0][0], {"X-Result-Count": "1"})
            else:
                self.binary(200, zip_files(files), "application/zip", "declaraciones_censuradas.zip", {"X-Result-Count": str(len(files))})
        except PermissionError:
            self.respond(401, {"error": "Invalid or expired session"})
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.respond(500, {"error": "Search and redaction failed"})
