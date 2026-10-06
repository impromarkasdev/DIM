from __future__ import annotations

import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter

from .config import Settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GraphClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.had_transient_failure = False
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=1)
        self.session.mount("https://", adapter)
        self.token = self._get_token()
        self.base = "https://graph.microsoft.com/v1.0"
        self.drive_base = f"{self.base}/drives/{settings.drive_id}" if settings.drive_id else (f"{self.base}/users/{settings.graph_user_id}/drive" if settings.graph_user_id else f"{self.base}/me/drive")

    def _get_token(self) -> str:
        url = f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
        }
        if self.settings.auth_mode == "client_credentials":
            data.update({"scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
        else:
            data.update({"refresh_token": self.settings.refresh_token, "scope": self.settings.azure_scopes, "grant_type": "refresh_token"})
        for attempt in range(1, 4):
            try:
                response = self.session.post(url, data=data, timeout=12)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 3:
                        retry_after = response.headers.get("Retry-After", "")
                        delay = min(float(retry_after), 5.0) if retry_after.isdigit() else float(attempt)
                        logger.warning("Azure token endpoint HTTP %s; retrying in %.1fs", response.status_code, delay)
                        time.sleep(delay)
                        continue
                response.raise_for_status()
                return str(response.json()["access_token"])
            except (requests.RequestException, KeyError, ValueError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                if attempt < 3 and retryable:
                    logger.warning("Azure token request failed; retry %s/3", attempt + 1)
                    time.sleep(float(attempt))
                    continue
                logger.error("Could not obtain Microsoft Graph token (HTTP %s)", status or "unavailable")
                raise RuntimeError("Could not obtain Microsoft Graph access token") from exc
        raise RuntimeError("Could not obtain Microsoft Graph access token")

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        attempts = 3
        request_timeout = kwargs.pop("timeout", 18)
        for attempt in range(1, attempts + 1):
            logger.info("Graph request start: %s %s (attempt %s/%s)", method, self._safe_log_url(url), attempt, attempts)
            try:
                response = self.session.request(method, url, headers=headers, timeout=request_timeout, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < attempts:
                        retry_after = response.headers.get("Retry-After", "")
                        delay = min(float(retry_after), 10.0) if retry_after.replace(".", "", 1).isdigit() else float(attempt)
                        logger.warning("Graph transient HTTP %s; retrying in %.1fs", response.status_code, delay)
                        time.sleep(delay)
                        continue
                response.raise_for_status()
                logger.info("Graph request complete: %s %s (HTTP %s)", method, self._safe_log_url(url), response.status_code)
                return response
            except requests.RequestException as exc:
                status = getattr(exc.response, "status_code", "unavailable")
                # GETs are idempotent. A short retry for 409 handles transient
                # SharePoint folder/index conflicts observed while enumerating
                # a hierarchy, without retrying conflicting writes.
                retryable = (
                    exc.response is None or status == 429 or status >= 500
                    or (status == 409 and method.upper() in {"GET", "PUT"})
                )
                if attempt < attempts and retryable:
                    delay = float(attempt)
                    logger.warning("Graph request failed (HTTP %s); retrying in %.1fs", status, delay)
                    time.sleep(delay)
                    continue
                if retryable:
                    self.had_transient_failure = True
                graph_code = ""
                if exc.response is not None:
                    try:
                        graph_code = str(exc.response.json().get("error", {}).get("code", ""))
                    except (ValueError, AttributeError):
                        pass
                if status == 409 and graph_code == "nameAlreadyExists" and method.upper() == "POST":
                    logger.info("Graph folder already exists; treating nameAlreadyExists as success")
                else:
                    logger.error("Graph request failed permanently (HTTP %s, code %s)", status, graph_code or "unknown")
                suffix = f", {graph_code}" if graph_code else ""
                raise RuntimeError(f"Microsoft Graph request failed ({status}{suffix})") from exc
        raise RuntimeError("Microsoft Graph request failed after retries")

    @staticmethod
    def _safe_log_url(url: str) -> str:
        """Log Graph endpoint shape without query values or shared-link tokens."""
        parsed = urlparse(url)
        path = parsed.path
        if "/shares/" in path:
            path = path.split("/shares/", 1)[0] + "/shares/[redacted]"
        return f"{parsed.netloc}{path}"

    def _request_details(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Graph error with status and a safe Graph code for API responses."""
        try:
            return self._request(method, url, **kwargs)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc

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

    @staticmethod
    def _shared_token(shared_url: str) -> str:
        return "u!" + base64.urlsafe_b64encode(shared_url.encode("utf-8")).decode("utf-8").rstrip("=")

    @staticmethod
    def _owner_from_personal_url(shared_url: str) -> tuple[str, str] | None:
        """Return owner email and OneDrive-internal path for /personal/<upn>/Documents/... URLs."""
        parsed = urlparse(shared_url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0].lower() != "personal":
            return None
        local = parts[1]
        if "_" not in local:
            return None
        # In OneDrive personal URLs, UPN uses '_' in place of '@' and dots.
        owner = local.replace("_", "@", 1).replace("_", ".")
        try:
            documents_index = next(index for index, value in enumerate(parts) if value.lower() == "documents")
        except StopIteration:
            return None
        relative = "/".join(parts[documents_index + 1:])
        return (owner, GraphClient._safe_path(relative)) if relative else None

    @staticmethod
    def _parent_path(parent_reference: dict[str, Any]) -> str:
        """Convert Graph parentReference.path to a root-relative safe path."""
        graph_path = str(parent_reference.get("path", ""))
        marker = "root:/"
        return GraphClient._safe_path(graph_path.split(marker, 1)[1]) if marker in graph_path else ""

    def resolve_file(self, file_path: str) -> tuple[bytes, str, str, str]:
        """Return bytes, filename, drive base and the source's parent folder path.

        Shared URL uses Graph shares first. A personal-site URL falls back to the
        owner's drive, which works when an application has Files.ReadWrite.All.
        """
        value = unquote(file_path).strip()
        if value.startswith("https://"):
            token = self._shared_token(value)
            try:
                item = self._request("GET", f"{self.base}/shares/{token}/driveItem?$select=id,name,parentReference").json()
                drive_id = str(item.get("parentReference", {}).get("driveId", ""))
                if drive_id and item.get("id"):
                    base = f"{self.base}/drives/{quote(drive_id, safe='')}"
                    content = self._request("GET", f"{base}/items/{quote(str(item['id']), safe='')}/content").content
                    return content, str(item.get("name", "documento.pdf")), base, self._parent_path(item.get("parentReference", {}))
            except RuntimeError:
                fallback = self._owner_from_personal_url(value)
                if not fallback:
                    raise
                owner, path = fallback
                base = f"{self.base}/users/{quote(owner, safe='@')}/drive"
                content = self._request("GET", f"{base}/root:/{quote(path, safe='/')}:/content").content
                return content, path.rsplit("/", 1)[-1], base, path.rsplit("/", 1)[0] if "/" in path else ""
            raise RuntimeError("Graph API returned an invalid shared DriveItem")
        if value.startswith("id:"):
            item_id = quote(value[3:].strip(), safe="")
            if not item_id: raise ValueError("DriveItem id cannot be empty")
            item = self._request("GET", f"{self.drive_base}/items/{item_id}?$select=name,parentReference").json()
            return self._request("GET", f"{self.drive_base}/items/{item_id}/content").content, str(item.get("name", "documento.pdf")), self.drive_base, self._parent_path(item.get("parentReference", {}))
        path = self._safe_path(value)
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        return self._request("GET", f"{self.drive_base}/root:/{quote(path, safe='/')}:/content").content, path.rsplit("/", 1)[-1], self.drive_base, parent

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
        cached = getattr(self, "_excel_cache", None)
        if cached is not None:
            logger.info("Graph Excel cache hit for this request")
            return cached
        path = quote(self.settings.excel_path.strip("/"), safe="/")
        logger.info("Downloading master Excel once for this request")
        content = self._request("GET", f"{self.drive_base}/root:/{path}:/content").content
        self._excel_cache = content
        logger.info("Master Excel download complete (%s bytes)", len(content))
        return content

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

    def list_children(self, folder: str, drive_base: str | None = None) -> list[dict[str, Any]]:
        """List one folder without relying on the user's current OneDrive root.

        Graph paginates large folders, therefore all pages are read before the
        caller decides which container or PDF it needs.
        """
        drive_base = drive_base or self.drive_base
        clean = self._safe_path(folder) if folder.strip("/") else ""
        if clean:
            encoded = quote(clean, safe="/")
            url = f"{drive_base}/root:/{encoded}:/children?$select=id,name,file,folder,parentReference,webUrl"
        else:
            url = f"{drive_base}/root/children?$select=id,name,file,folder,parentReference,webUrl"
        children: list[dict[str, Any]] = []
        while url:
            payload = self._request("GET", url).json()
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise RuntimeError("Microsoft Graph returned an invalid folder listing")
            children.extend(item for item in values if isinstance(item, dict))
            next_url = payload.get("@odata.nextLink")
            url = str(next_url) if next_url else ""
        return children

    def list_item_children(self, item_id: str, drive_base: str | None = None) -> list[dict[str, Any]]:
        """List a known folder by DriveItem ID to avoid fragile path addressing."""
        drive_base = drive_base or self.drive_base
        safe_id = quote(item_id.strip(), safe="")
        if not safe_id:
            raise ValueError("DriveItem id cannot be empty")
        url = f"{drive_base}/items/{safe_id}/children?$select=id,name,file,folder,parentReference,webUrl"
        children: list[dict[str, Any]] = []
        while url:
            payload = self._request("GET", url).json()
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise RuntimeError("Microsoft Graph returned an invalid folder listing")
            children.extend(item for item in values if isinstance(item, dict))
            next_url = payload.get("@odata.nextLink")
            url = str(next_url) if next_url else ""
        return children

    def search_items(self, query: str, drive_base: str | None = None) -> list[dict[str, Any]]:
        """Search the selected OneDrive/SharePoint drive, following Graph pages.

        This is a drive-wide indexed search, not a recursive crawl. Callers must
        still validate returned names/paths against authoritative Excel data.
        """
        drive_base = drive_base or self.drive_base
        query = query.strip()
        if not query:
            return []
        encoded_query = quote(query, safe="")
        url = (
            f"{drive_base}/root/search(q='{encoded_query}')"
            "?$select=id,name,file,folder,parentReference,webUrl,size&$top=100"
        )
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            payload = self._request("GET", url).json()
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise RuntimeError("Microsoft Graph returned an invalid search result")
            for item in values:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id", ""))
                key = item_id or f"{item.get('name', '')}:{item.get('webUrl', '')}"
                if key not in seen:
                    seen.add(key)
                    results.append(item)
                    if len(results) >= 100:
                        url = ""
                        break
            next_url = payload.get("@odata.nextLink")
            if url:
                url = str(next_url) if next_url else ""
        logger.info("Graph drive search returned %s items for query %r", len(results), query)
        return results

    def download_item(self, item: dict[str, Any], drive_base: str | None = None) -> bytes:
        """Download a search/list result by DriveItem ID, avoiding path ambiguity."""
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            raise ValueError("Graph DriveItem has no id")
        item_drive = str(item.get("parentReference", {}).get("driveId", ""))
        selected_drive = f"{self.base}/drives/{quote(item_drive, safe='')}" if item_drive else (drive_base or self.drive_base)
        return self._request(
            "GET", f"{selected_drive}/items/{quote(item_id, safe='')}/content"
        ).content

    def ensure_folder(self, folder: str, drive_base: str | None = None) -> None:
        drive_base = drive_base or self.drive_base
        current = ""
        for segment in self._safe_path(folder).split("/"):
            parent = quote(current, safe="/")
            url = f"{drive_base}/root:/{parent}:/children" if current else f"{drive_base}/root/children"
            try:
                self._request("POST", url, json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
            except RuntimeError as exc:
                # Only an existing-name conflict is expected. Do not hide
                # permission, network, or server failures as if the folder existed.
                if "Microsoft Graph request failed (409" not in str(exc):
                    raise
            current = f"{current}/{segment}" if current else segment

    def upload_pdf(self, path: str, content: bytes, drive_base: str | None = None, ensure_parent: bool = True) -> dict[str, Any]:
        drive_base = drive_base or self.drive_base
        clean = self._safe_path(path)
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        if parent and ensure_parent: self.ensure_folder(parent, drive_base)
        encoded = quote(clean, safe="/")
        uploaded = self._request("PUT", f"{drive_base}/root:/{encoded}:/content", data=content, headers={"Content-Type": "application/pdf"}).json()
        if not uploaded.get("@microsoft.graph.downloadUrl") and uploaded.get("id"):
            item_id = quote(str(uploaded["id"]), safe="")
            metadata_url = f"{drive_base}/items/{item_id}?$select=id,name,@microsoft.graph.downloadUrl"
            metadata = {}
            for attempt in range(3):
                try:
                    metadata = self._request("GET", metadata_url).json()
                    break
                except RuntimeError:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            uploaded.update(metadata)
        if not uploaded.get("@microsoft.graph.downloadUrl"):
            raise RuntimeError("Microsoft Graph uploaded the PDF but did not return a download URL")
        return uploaded

    def upload_many_pdfs(self, folder: str, files: list[tuple[str, bytes]], max_workers: int = 10, drive_base: str | None = None) -> list[dict[str, Any]]:
        drive_base = drive_base or self.drive_base
        self.ensure_folder(folder, drive_base)
        def upload(file: tuple[str, bytes]) -> dict[str, Any]:
            name, content = file
            if "/" in name or "\\" in name: raise ValueError("Output filename must not contain a path")
            return self.upload_pdf(f"{folder}/{name}", content, drive_base, ensure_parent=False)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(files) or 1)) as executor:
            return list(executor.map(upload, files))
