from __future__ import annotations
from urllib.parse import parse_qs, urlparse

from app.http_api import ApiHandler
from app.users import delete_user, list_users, upsert_user

class handler(ApiHandler):
    def do_GET(self) -> None:
        try: self.claims(admin=True); self.respond(200, {"users": list_users()})
        except ValueError: self.respond(401, {"error": "Invalid or expired session"})
        except PermissionError: self.respond(403, {"error": "Forbidden"})
        except Exception: self.respond(500, {"error": "User listing failed"})
    def do_POST(self) -> None:
        try:
            self.claims(admin=True)
        except ValueError:
            self.respond(401, {"error":"Invalid or expired session"}); return
        except PermissionError:
            self.respond(403, {"error":"Forbidden"}); return
        try:
            body=self.json_body()
            user = upsert_user(str(body["email"]), body.get("password"), str(body.get("role", "client")), bool(body.get("active", True)))
            self.respond(200, {"status":"saved", "user": user})
        except (KeyError, ValueError): self.respond(400, {"error":"Invalid user payload"})
    def do_DELETE(self) -> None:
        try:
            self.claims(admin=True)
            user_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not user_id: raise ValueError("id query parameter is required")
            if not delete_user(user_id): self.respond(404, {"error": "User not found or is an administrator"}); return
            self.respond(200, {"status": "deleted", "id": user_id})
        except PermissionError: self.respond(403,{"error":"Forbidden"})
        except ValueError as exc: self.respond(400,{"error":str(exc)})
