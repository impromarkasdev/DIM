from __future__ import annotations

import traceback

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler
from app.pdf_splitter import split_pages


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims(admin=True)
            file_path = str(self.json_body().get("filePath", "")).strip()
            if not (file_path.lower().endswith(".pdf") or file_path.startswith("id:")):
                raise ValueError("filePath must be a PDF path or an id:<DriveItem-id> value")
            graph = GraphClient(Settings.from_env())
            files = split_pages(graph.download_path(file_path), file_path.rsplit("/", 1)[-1] if not file_path.startswith("id:") else "documento.pdf")
            for filename, content in files:
                graph.upload_pdf(f"Procesados/{filename}", content)
            self.respond(200, {"success": True, "count": len(files), "message": "PDF dividido con éxito"})
        except PermissionError:
            self.respond(403, {"error": "Administrator access required"})
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.respond(500, {"success": False, "message": "PDF splitting failed"})
