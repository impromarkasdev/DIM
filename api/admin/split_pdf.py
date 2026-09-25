from __future__ import annotations

import cgi
import traceback

from app.downloads import zip_files
from app.http_api import ApiHandler
from app.pdf_splitter import split_declarations


class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            self.claims(admin=True)
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("Content-Type must be multipart/form-data")
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
            field = form["file"] if "file" in form else None
            if not field or not getattr(field, "file", None):
                raise ValueError("Multipart field 'file' is required")
            raw = field.file.read()
            if not raw or len(raw) > 45_000_000:
                raise ValueError("PDF must be between 1 byte and 45 MB")
            files = split_declarations(raw)
            self.binary(200, zip_files(files), "application/zip", "declaraciones_DIM.zip", {"X-Declaration-Count": str(len(files))})
        except PermissionError:
            self.respond(403, {"error": "Administrator access required"})
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.respond(500, {"error": "PDF splitting failed"})
