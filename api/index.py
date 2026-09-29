"""Central Vercel Python entrypoint; imports route handlers only when requested."""
from __future__ import annotations

import importlib
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from app.http_api import ApiHandler


ROUTES = {
    "/api/auth/login": "api.auth.login",
    "/api/admin/users": "api.admin.users",
    "/api/admin/split-pdf": "api.admin.split_pdf",
    "/api/documents/process-redact": "api.dim.search_redact",
    "/api/dim/search-and-redact": "api.dim.search_redact",
    "/api/cron/keep-alive": "api.cron.keep_alive",
    "/api/process": "api.process",
}


class handler(ApiHandler):
    """Dispatches each request to an existing BaseHTTPRequestHandler class."""

    # Compatibility with process.py and keep_alive.py legacy response helper.
    _respond = ApiHandler.respond

    def _dispatch(self, action_name: str) -> None:
        request_path = urlparse(self.path).path
        module_name = ROUTES.get(request_path)
        if not module_name:
            self.respond(404, {"success": False, "message": "Route not found"})
            return
        try:
            target: type[BaseHTTPRequestHandler] = getattr(importlib.import_module(module_name), "handler")
            action = getattr(target, action_name, None)
            if not callable(action):
                self.respond(405, {"success": False, "message": "Method not allowed"})
                return
            action(self)
        except Exception as exc:
            self.respond(500, {"success": False, "message": f"Route initialization failed: {type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        self._dispatch("do_GET")

    def do_POST(self) -> None:
        self._dispatch("do_POST")

    def do_DELETE(self) -> None:
        self._dispatch("do_DELETE")
