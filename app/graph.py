from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests

from .config import Settings


class GraphClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.token = self._get_token()
        self.base = "https://graph.microsoft.com/v1.0"
        self.drive_base = f"{self.base}/drives/{settings.drive_id}" if settings.drive_id else (f"{self.base}/users/{settings.graph_user_id}/drive" if settings.graph_user_id else f"{self.base}/me/drive")

    def _get_token(self) -> str:
        url = f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token"
        try:
            data = {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            }
            if self.settings.auth_mode == "client_credentials":
                data.update({"scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
            else:
                data.update({"refresh_token": self.settings.refresh_token, "scope": self.settings.azure_scopes, "grant_type": "refresh_token"})
            response = self.session.post(url, data=data, timeout=20)
            response.raise_for_status()
            return str(response.json()["access_token"])
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise RuntimeError("Could not obtain Microsoft Graph access token") from exc

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.request(method, url, headers=headers, timeout=45, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", "unavailable")
            raise RuntimeError(f"Microsoft Graph request failed ({status})") from exc

    @staticmethod
    def _safe_path(value: str) -> str:
        path = unquote(value).strip().replace("\\", "/")
        if not path or "\x00" in path:
            raise ValueError("A non-empty OneDrive path is required")
        parts = PurePosixPath("/" + path.lstrip("/")).parts
        if any(part in {"..", "."} for part in parts):
            raise ValueError("Path traversal is not allowed")
        return "/".join(part for part in parts if part != "/")

    def _shared_content_url(self, shared_url: str) -> str:
        parsed = urlparse(shared_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Only valid HTTPS SharePoint or OneDrive URLs are allowed")
        encoded = base64.urlsafe_b64encode(shared_url.encode()).decode().rstrip("=")
        return f"{self.base}/shares/u!{encoded}/driveItem/content"

    def connection_status(self) -> dict[str, str]:
        """Validate delegated identity, selected OneDrive and workbook access without returning secrets."""
        try:
            drive = self._request("GET", f"{self.drive_base}?$select=id,driveType,owner,webUrl").json()
            path = quote(self.settings.excel_path.strip("/"), safe="/")
            item = self._request("GET", f"{self.drive_base}/root:/{path}?$select=id,name,size,file").json()
            return {
                "estado": "Conectado",
                "usuario": self.settings.graph_user_id or "application-access",
                "drive_id": str(drive.get("id", "")),
                "drive_type": str(drive.get("driveType", "")),
                "archivo_excel": str(item.get("name", "")),
                "excel_bytes": str(item.get("size", 0)),
            }
        except Exception as exc:
            raise RuntimeError("OneDrive connection verification failed") from exc

    def keep_alive(self) -> dict[str, str]:
        """Refresh delegated credentials and perform a minimal authenticated Graph call."""
        try:
            drive = self._request("GET", f"{self.drive_base}?$select=id").json()
            if not drive.get("id"):
                raise RuntimeError("Microsoft Graph did not return a drive")
            return {"estado": "Token renovado y Graph disponible"}
        except Exception as exc:
            raise RuntimeError("OneDrive keep-alive failed") from exc

    def download_excel(self) -> bytes:
        """Download the workbook once; local lookup avoids Graph calls per worksheet."""
        path = quote(self.settings.excel_path.strip("/"), safe="/")
        return self._request("GET", f"{self.drive_base}/root:/{path}:/content").content

    def download_original(self, pdf_name: str) -> bytes:
        return self.download_path(f"Originales/{pdf_name}")

    def download_path(self, path: str) -> bytes:
        path = path.strip()
        if path.startswith("https://"):
            return self._request("GET", self._shared_content_url(path)).content
        if path.startswith("id:"):
            item_id = quote(path[3:].strip(), safe="")
            if not item_id:
                raise ValueError("DriveItem id cannot be empty")
            return self._request("GET", f"{self.drive_base}/items/{item_id}/content").content
        encoded = quote(self._safe_path(path), safe="/")
        return self._request("GET", f"{self.drive_base}/root:/{encoded}:/content").content

    def ensure_folder(self, folder: str) -> None:
        current = ""
        for segment in self._safe_path(folder).split("/"):
            parent = quote(current, safe="/")
            url = f"{self.drive_base}/root:/{parent}:/children" if current else f"{self.drive_base}/root/children"
            try:
                self._request("POST", url, json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
            except RuntimeError:
                # 409 is expected when an earlier request already created the folder.
                pass
            current = f"{current}/{segment}" if current else segment

    def upload_pdf(self, path: str, content: bytes) -> dict[str, Any]:
        clean = self._safe_path(path)
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        if parent: self.ensure_folder(parent)
        encoded = quote(clean, safe="/")
        return self._request("PUT", f"{self.drive_base}/root:/{encoded}:/content", data=content, headers={"Content-Type": "application/pdf"}).json()

    def upload_many_pdfs(self, folder: str, files: list[tuple[str, bytes]], max_workers: int = 4) -> list[dict[str, Any]]:
        self.ensure_folder(folder)
        def upload(file: tuple[str, bytes]) -> dict[str, Any]:
            name, content = file
            if "/" in name or "\\" in name: raise ValueError("Output filename must not contain a path")
            return self.upload_pdf(f"{folder}/{name}", content)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(files) or 1)) as executor:
            return list(executor.map(upload, files))
