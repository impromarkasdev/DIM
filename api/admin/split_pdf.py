from __future__ import annotations

import logging

from app.config import Settings
from app.graph import GraphClient
from app.http_api import ApiHandler
from app.pdf_splitter import split_declarations

logger = logging.getLogger(__name__)


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims(admin=True)
            file_path = str(self.json_body().get("filePath", "")).strip()
            if not (file_path.lower().endswith(".pdf") or file_path.startswith(("id:", "https://"))):
                raise ValueError("filePath must be a PDF path or an id:<DriveItem-id> value")
            graph = GraphClient(Settings.from_env())
            logger.info("PDF split: downloading source from Graph")
            content, filename, source_drive, parent_folder = graph.resolve_file(file_path)
            logger.info("PDF split: source download complete (%s, %s bytes)", filename, len(content))
            files = split_declarations(content)
            output_folder = f"{parent_folder}/Procesados" if parent_folder else "Procesados"
            logger.info("PDF split: uploading %s grouped declarations to %s", len(files), output_folder)
            graph.upload_many_pdfs(output_folder, files, drive_base=source_drive)
            logger.info("PDF split: upload complete")
            self.respond(200, {
                "success": True,
                "count": len(files),
                "message": "PDF dividido con éxito",
                "outputFolder": f"/{output_folder}",
                "files": [name for name, _ in files],
            })
        except PermissionError:
            self.respond(403, {"success": False, "message": "Administrator access required"})
        except ValueError as exc:
            logger.info("PDF split rejected: %s", exc)
            self.respond(400, {"success": False, "message": str(exc)})
        except RuntimeError as exc:
            logger.exception("PDF split Graph error")
            status = 503 if "OCR" in str(exc) else 502
            self.respond(status, {"success": False, "message": str(exc)})
        except Exception as exc:
            logger.exception("Unexpected PDF split error")
            self.respond(500, {"success": False, "message": f"PDF splitting failed: {exc}"})
