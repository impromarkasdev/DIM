"""Single Vercel Serverless entrypoint with lazy, path-based handler dispatch."""
from __future__ import annotations

import importlib
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.http_api import ApiHandler


ROUTES: dict[str, str] = {
    "/api/process": "api.process",
    "/api/auth/login": "api.auth.login",
    "/api/admin/users": "api.admin.users",
    "/api/admin/split-pdf": "api.admin.split_pdf",
    "/api/documents/process-redact": "api.dim.search_redact",
    "/api/dim/search-and-redact": "api.dim.search_redact",
    "/api/cron/keep-alive": "api.cron.keep_alive",
}


class handler(ApiHandler):
    """Delegates the original request object to individual BaseHTTPRequestHandlers."""

    _respond = ApiHandler.respond

    def _request_path(self) -> str:
        parsed = urlparse(self.path)
        return parse_qs(parsed.query).get("path", [parsed.path])[0]

    def _dispatch(self, method: str) -> None:
        route = self._request_path()
        module_name = ROUTES.get(route)
        if not module_name:
            self.respond(404, {"success": False, "message": f"Route not found: {route}"})
            return
        try:
            module = importlib.import_module(module_name)
            target: type[BaseHTTPRequestHandler] = getattr(module, "handler")
            action = getattr(target, method, None)
            if not callable(action):
                self.respond(405, {"success": False, "message": f"Method not allowed: {method[3:]}"})
                return
            action(self)
        except Exception as exc:
            self.respond(500, {"success": False, "message": f"Dispatcher error ({module_name}): {type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        self._dispatch("do_GET")

    def do_POST(self) -> None:
        self._dispatch("do_POST")

    def do_DELETE(self) -> None:
        self._dispatch("do_DELETE")
