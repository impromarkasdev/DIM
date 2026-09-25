from __future__ import annotations

import hmac
import json
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any

from app.config import Settings
from app.graph import GraphClient
from app.service import process_references


class handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 1_000_000:
                self._respond(400, {"error": "A JSON body up to 1 MB is required"})
                return
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if isinstance(payload, list):
                payload = {"referencias": payload}
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object or an array of references")
            settings = Settings.from_env()
            received_secret = self.headers.get("X-Webhook-Secret", "")
            if settings.webhook_secret and not hmac.compare_digest(received_secret, settings.webhook_secret):
                self._respond(401, {"error": "Unauthorized webhook"})
                return
            result = process_references(payload, settings)
            self._respond(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self._respond(500, {"error": "Document batch processing failed"})

    def do_GET(self) -> None:
        try:
            settings = Settings.from_env()
            received_secret = self.headers.get("X-Webhook-Secret", "")
            if not settings.webhook_secret or not hmac.compare_digest(received_secret, settings.webhook_secret):
                self._respond(401, {"error": "Unauthorized webhook"})
                return
            self._respond(200, GraphClient(settings).connection_status())
        except Exception:
            traceback.print_exc()
            self._respond(502, {"error": "OneDrive connection verification failed"})
