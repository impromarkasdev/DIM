from __future__ import annotations

import traceback

from app.http_api import ApiHandler
from app.config import Settings
from app.graph import GraphClient
from app.service import process_references


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims()
            body = self.json_body()
            references = body.get("references", body.get("referencias"))
            if isinstance(references, str): references = [references]
            if not isinstance(references, list): raise ValueError("references must be a string or array")
            result = process_references({"referencias": references, "datosSensibles": body.get("datosSensibles", [])}, Settings.from_env())
            successful = [item for item in result["resultados"] if item.get("estado") == "Exito"]
            if not successful:
                self.respond(404, {"success": False, "message": "No matching PDF was found", "resultados": result["resultados"]})
                return
            graph = GraphClient(Settings.from_env())
            uploaded = []
            import base64
            files = [(f"redactada_{item['nombre_pdf']}", base64.b64decode(item["pdf_base64"])) for item in successful]
            metadata_list = graph.upload_many_pdfs("Procesados", files)
            for (filename, _), metadata in zip(files, metadata_list):
                uploaded.append({"fileName": filename, "downloadUrl": metadata.get("@microsoft.graph.downloadUrl", "")})
            self.respond(200, {"success": True, "downloadUrl": uploaded[0]["downloadUrl"], "fileName": uploaded[0]["fileName"], "files": uploaded})
        except PermissionError:
            self.respond(401, {"success": False, "message": "Invalid or expired session"})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            self.respond(500, {"success": False, "message": "Search and redaction failed"})
