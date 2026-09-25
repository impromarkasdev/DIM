from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from typing import Any

from .security import decode_jwt


class ApiHandler(BaseHTTPRequestHandler):
    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        allowed = {item.strip() for item in os.getenv("CORS_ORIGINS", "https://impromarkas.com,https://www.impromarkas.com").split(",")}
        return origin if origin in allowed else None

    def respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin); self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def binary(self, status: int, content: bytes, content_type: str, filename: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin); self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Declaration-Count, X-Result-Count")
        for key, value in (extra_headers or {}).items(): self.send_header(key, value)
        self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)

    def do_OPTIONS(self) -> None:
        self.respond(204, {})

    def json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1_000_000:
            raise ValueError("A JSON body up to 1 MB is required")
        data = json.loads(self.rfile.read(length).decode())
        if not isinstance(data, dict): raise ValueError("JSON body must be an object")
        return data

    def claims(self, admin: bool = False) -> dict[str, Any]:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        claims = decode_jwt(token, os.getenv("JWT_SECRET", ""))
        if admin and claims.get("role") != "admin": raise PermissionError("Administrator access required")
        return claims
