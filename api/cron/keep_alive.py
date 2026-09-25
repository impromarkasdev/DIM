from __future__ import annotations

import hmac
import json
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any

from app.config import Settings
from app.graph import GraphClient


class handler(BaseHTTPRequestHandler):
    """Vercel Cron endpoint. Vercel supplies Authorization: Bearer <CRON_SECRET>."""

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            settings = Settings.from_env()
            expected = f"Bearer {settings.cron_secret}"
            received = self.headers.get("Authorization", "")
            if not settings.cron_secret or not hmac.compare_digest(received, expected):
                self._respond(401, {"error": "Unauthorized cron request"})
                return
            self._respond(200, {"success": True, **GraphClient(settings).keep_alive()})
        except Exception:
            traceback.print_exc()
            self._respond(502, {"success": False, "error": "Azure keep-alive failed"})

    def do_POST(self) -> None:
        self._respond(405, {"error": "Use GET"})
