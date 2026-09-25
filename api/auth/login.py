from __future__ import annotations
from app.http_api import ApiHandler
from app.security import create_jwt
from app.users import authenticate
import os

class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            body = self.json_body(); email = str(body.get("email", "")); password = str(body.get("password", ""))
            user = authenticate(email, password)
            if not user: self.respond(401, {"error": "Invalid credentials"}); return
            self.respond(200, {"token": create_jwt({"sub": user["email"], "role": user["role"]}, os.environ["JWT_SECRET"]), "role": user["role"]})
        except (ValueError, RuntimeError): self.respond(400, {"error": "Invalid login request"})
