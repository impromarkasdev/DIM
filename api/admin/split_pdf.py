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
            if not (file_path.lower().endswith(".pdf") or file_path.startswith(("id:", "https://"))):
                raise ValueError("filePath must be a PDF path or an id:<DriveItem-id> value")
            graph = GraphClient(Settings.from_env())
            files = split_pages(graph.download_path(file_path), file_path.rsplit("/", 1)[-1] if not file_path.startswith("id:") else "documento.pdf")
            graph.upload_many_pdfs("Procesados", files)
            self.respond(200, {"success": True, "count": len(files), "message": "PDF dividido con éxito"})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Administrator access required"})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except Exception:
            self.respond(500, {"success": False, "message": "PDF splitting failed"})
