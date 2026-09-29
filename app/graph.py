from __future__ import annotations

from typing import Any
from urllib.parse import quote

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
            raise RuntimeError(f"Microsoft Graph request failed: {method} {url}") from exc

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
        if path.startswith("id:"):
            item_id = quote(path[3:].strip(), safe="")
            if not item_id:
                raise ValueError("DriveItem id cannot be empty")
            return self._request("GET", f"{self.drive_base}/items/{item_id}/content").content
        encoded = quote(path.strip("/"), safe="/")
        return self._request("GET", f"{self.drive_base}/root:/{encoded}:/content").content

    def upload_pdf(self, path: str, content: bytes) -> dict[str, Any]:
        encoded = quote(path.strip("/"), safe="/")
        return self._request("PUT", f"{self.drive_base}/root:/{encoded}:/content", data=content, headers={"Content-Type": "application/pdf"}).json()
