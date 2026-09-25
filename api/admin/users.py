from __future__ import annotations
from app.http_api import ApiHandler
from app.users import delete_user, list_users, upsert_user

class handler(ApiHandler):
    def do_GET(self) -> None:
        try: self.claims(admin=True); self.respond(200, {"users": list_users()})
        except PermissionError: self.respond(403, {"error": "Forbidden"})
        except Exception: self.respond(500, {"error": "User listing failed"})
    def do_POST(self) -> None:
        try:
            self.claims(admin=True); body=self.json_body(); upsert_user(str(body["email"]), body.get("password"), str(body.get("role", "client")), bool(body.get("active", True))); self.respond(200, {"status":"saved"})
        except (KeyError, ValueError): self.respond(400, {"error":"Invalid user payload"})
        except PermissionError: self.respond(403, {"error":"Forbidden"})
    def do_DELETE(self) -> None:
        try: self.claims(admin=True); delete_user(self.path.split("email=",1)[1]); self.respond(200,{"status":"deleted"})
        except (PermissionError, IndexError): self.respond(403,{"error":"Forbidden"})
