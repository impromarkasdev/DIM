"""Single Vercel Python entrypoint that dispatches the public REST API routes."""
from __future__ import annotations

from typing import Type
from urllib.parse import parse_qs, urlparse

from app.http_api import ApiHandler
from api.admin.split_pdf import handler as SplitPdfHandler
from api.admin.users import handler as UsersHandler
from api.auth.login import handler as LoginHandler
from api.cron.keep_alive import handler as KeepAliveHandler
from api.dim.search_redact import handler as SearchRedactHandler
from api.process import handler as LegacyProcessHandler


class handler(ApiHandler):
    """Route requests to legacy BaseHTTPRequestHandler-compatible handlers."""

    _respond = ApiHandler.respond

    def _target(self) -> Type[ApiHandler] | Type[KeepAliveHandler] | None:
        endpoint = parse_qs(urlparse(self.path).query).get("endpoint", [""])[0]
        return {
            "process": LegacyProcessHandler,
            "keep-alive": KeepAliveHandler,
            "login": LoginHandler,
            "users": UsersHandler,
            "split-pdf": SplitPdfHandler,
            "search-redact": SearchRedactHandler,
        }.get(endpoint)

    def _dispatch(self, method: str) -> None:
        target = self._target()
        action = getattr(target, method, None) if target else None
        if not action:
            self.respond(404, {"success": False, "message": "API route not found"})
            return
        action(self)

    def do_GET(self) -> None:
        self._dispatch("do_GET")

    def do_POST(self) -> None:
        self._dispatch("do_POST")

    def do_DELETE(self) -> None:
        self._dispatch("do_DELETE")
