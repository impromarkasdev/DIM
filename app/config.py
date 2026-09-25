from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    refresh_token: str
    azure_scopes: str
    drive_id: str
    excel_path: str
    lookup_column_index: int
    pdf_column_index: int
    location_column_index: int
    first_year: int
    max_response_pdf_bytes: int
    webhook_secret: str
    cron_secret: str
    standard_sensitive_terms: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        names = {
            "tenant_id": "AZURE_TENANT_ID", "client_id": "AZURE_CLIENT_ID",
            "client_secret": "AZURE_CLIENT_SECRET", "refresh_token": "AZURE_REFRESH_TOKEN",
            "excel_path": "EXCEL_PATH",
        }
        values = {field: os.getenv(env_name, "").strip() for field, env_name in names.items()}
        missing = [env_name for field, env_name in names.items() if not values[field]]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        try:
            indexes = {
                "lookup_column_index": int(os.getenv("EXCEL_LOOKUP_COLUMN_INDEX", "0")),
                "pdf_column_index": int(os.getenv("EXCEL_PDF_COLUMN_INDEX", "2")),
                "location_column_index": int(os.getenv("EXCEL_LOCATION_COLUMN_INDEX", "3")),
                "first_year": int(os.getenv("EXCEL_FIRST_YEAR", "2021")),
                "max_response_pdf_bytes": int(os.getenv("MAX_RESPONSE_PDF_BYTES", "3000000")),
            }
        except ValueError as exc:
            raise RuntimeError("Excel column indexes and limits must be integers") from exc
        if min(indexes.values()) < 0 or indexes["first_year"] < 2000 or indexes["max_response_pdf_bytes"] <= 0:
            raise RuntimeError("Invalid Excel lookup indexes or response limit")
        terms = tuple(term.strip() for term in os.getenv("STANDARD_SENSITIVE_TERMS", "").split(",") if term.strip())
        # Omit GRAPH_DRIVE_ID for the delegated user's personal OneDrive.
        values["drive_id"] = os.getenv("GRAPH_DRIVE_ID", "").strip()
        values["azure_scopes"] = os.getenv(
            "AZURE_SCOPES", "offline_access User.Read Files.ReadWrite"
        ).strip()
        if not values["azure_scopes"]:
            raise RuntimeError("AZURE_SCOPES cannot be empty")
        return cls(
            **values,
            **indexes,
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            cron_secret=os.getenv("CRON_SECRET", ""),
            standard_sensitive_terms=terms,
        )
