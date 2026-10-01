from __future__ import annotations

import logging

from app.http_api import ApiHandler
from app.config import Settings
from app.service import process_hierarchical_references

logger = logging.getLogger(__name__)


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims()
            body = self.json_body()
            references = body.get("references", body.get("referencias"))
            if isinstance(references, str): references = [references]
            if not isinstance(references, list): raise ValueError("references must be a string or array")
            sensitive_terms = body.get("datosSensibles", [])
            if not isinstance(sensitive_terms, list): raise ValueError("datosSensibles must be an array of strings")
            result = process_hierarchical_references(references, sensitive_terms, Settings.from_env())
            successful = [item for item in result["resultados"] if item.get("estado") == "Exito"]
            if not successful:
                self.respond(404, {"success": False, "message": "No matching PDF was found", "resultados": result["resultados"]})
                return
            files = [{"fileName": item["nombre_pdf"], "downloadUrl": item["downloadUrl"], "outputFolder": item["outputFolder"]} for item in successful]
            self.respond(200, {"success": True, "downloadUrl": files[0]["downloadUrl"], "fileName": files[0]["fileName"], "files": files, "resultados": result["resultados"]})
        except PermissionError:
            self.respond(401, {"success": False, "message": "Invalid or expired session"})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception as exc:
            logger.exception("Unexpected DIM search/redaction error")
            self.respond(500, {"success": False, "message": f"Search and redaction failed: {exc}"})
