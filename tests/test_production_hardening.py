from __future__ import annotations

import unittest
from unittest.mock import patch
import pandas as pd

from app.graph import GraphClient
from app.errors import PermanentProcessingError
from app.pdf_service import redact_pdf
from app.security import create_jwt, decode_jwt
from app.service import _excel_columns, _filename_matches_form, _safe_graph_search, _sheet_excel_columns
from api.admin.users import _parse_active
from app.queue_worker import process_dim_reference
from vercel.queue import get_subscriptions
from api.dim.jobs import _refresh_download_links


class GraphSearchTests(unittest.TestCase):
    def test_excel_reference_columns_shift_after_2023(self) -> None:
        self.assertEqual(_excel_columns("2023"), (41, 42, 43))  # AP/AQ/AR
        self.assertEqual(_excel_columns("2024"), (42, 43, 44))  # AQ/AR/AS

    def test_excel_headers_override_year_column_fallback(self) -> None:
        # Current workbook sheets have drifted: actual headers are more reliable
        # than a year-only offset (2026 AP/AQ/AR vs 2024 AQ/AR/AS).
        current = pd.DataFrame([["NUMERO_FORMULARIO", "REFERENCIA", "EMBARQUE", "NUMERO_FORMULARIO"]])
        older = pd.DataFrame([["NUMERO_FORMULARIO", "", "REFERENCIA", "EMBARQUE", "NUMERO_FORMULARIO"]])
        self.assertEqual(_sheet_excel_columns(current, "2026"), (0, 1, 2, 3))
        self.assertEqual(_sheet_excel_columns(older, "2023"), (0, 2, 3, 4))

    def test_filename_match_accepts_optional_check_digit_only(self) -> None:
        self.assertTrue(_filename_matches_form("_032026001406316-1.pdf", "032026001406316"))
        self.assertTrue(_filename_matches_form("032026001406316-1.pdf", "032026001406316-1"))
        self.assertFalse(_filename_matches_form("032026001406316-backup.pdf", "032026001406316"))

    def test_graph_client_never_sends_bearer_token_to_another_host(self) -> None:
        graph = object.__new__(GraphClient)
        graph.token = "test-token"
        graph.session = type(
            "Session",
            (),
            {"request": lambda *args, **kwargs: self.fail("HTTP request must not be sent")},
        )()
        with self.assertRaisesRegex(ValueError, "Microsoft Graph"):
            graph._request("GET", "https://attacker.example/v1.0/me/drive")

    def test_search_follows_all_pages_beyond_previous_100_item_cap(self) -> None:
        graph = object.__new__(GraphClient)
        graph.base = "https://graph.microsoft.com/v1.0"
        graph.drive_base = "https://graph.microsoft.com/v1.0/drives/test-drive"
        pages = [
            {
                "value": [{"id": str(index), "name": f"{index}.pdf", "file": {}} for index in range(100)],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/test-drive/root/search(q='form')?$skiptoken=next",
            },
            {"value": [{"id": str(index), "name": f"{index}.pdf", "file": {}} for index in range(100, 130)]},
        ]
        requested: list[str] = []

        def fake_request(method: str, url: str, **kwargs: object) -> object:
            self.assertEqual(method, "GET")
            requested.append(url)
            payload = pages.pop(0)
            return type("Response", (), {"json": lambda self: payload})()

        graph._request = fake_request  # type: ignore[method-assign]
        items = graph.search_items("form")
        self.assertEqual(len(items), 130)
        self.assertEqual(len(requested), 2)

    def test_graph_search_errors_are_not_converted_to_empty_results(self) -> None:
        graph = type("SearchGraph", (), {"search_items": lambda self, query: (_ for _ in ()).throw(RuntimeError("Graph unavailable"))})()
        with self.assertRaisesRegex(RuntimeError, "Graph unavailable"):
            _safe_graph_search(graph, "reference")

    def test_completed_job_status_survives_temporary_download_link_failure(self) -> None:
        job = {
            "status": "completed",
            "resultados": [{"estado": "Exito", "downloadUrl": "/api/documents/download?jobId=x"}],
        }
        target = {"driveItemId": "drive-item", "driveBase": "https://graph.microsoft.com/v1.0/drives/drive"}
        with patch("api.dim.jobs.get_download_target", return_value=target), \
             patch("api.dim.jobs.Settings.from_env", return_value=object()), \
             patch("api.dim.jobs.GraphClient", side_effect=RuntimeError("Graph unavailable")):
            _refresh_download_links(job, "job-id", "client@example.com", False)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["resultados"][0]["estado"], "Exito")
        self.assertIsNone(job["resultados"][0]["downloadUrl"])
        self.assertTrue(job["resultados"][0]["downloadLinkPending"])


class SecurityTests(unittest.TestCase):
    def test_jwt_requires_strong_secret_and_valid_claims(self) -> None:
        with self.assertRaises(ValueError):
            create_jwt({"sub": "user@example.com", "role": "client"}, "weak")
        secret = "s" * 48
        token = create_jwt({"sub": "user@example.com", "role": "client"}, secret)
        self.assertEqual(decode_jwt(token, secret)["role"], "client")
        with self.assertRaises(ValueError):
            decode_jwt(token, "x" * 48)

    def test_active_flag_rejects_ambiguous_values(self) -> None:
        self.assertFalse(_parse_active("false"))
        self.assertTrue(_parse_active("true"))
        with self.assertRaises(ValueError):
            _parse_active("False maybe")


class QueueConfigurationTests(unittest.TestCase):
    def test_worker_entrypoint_registers_expected_queue_subscription(self) -> None:
        subscriptions = get_subscriptions()
        worker_subscriptions = [
            subscription for subscription in subscriptions
            if subscription.func is process_dim_reference
        ]
        self.assertEqual(len(worker_subscriptions), 1)
        self.assertEqual(worker_subscriptions[0].topic, "dim-redaction")


class RedactionSafetyTests(unittest.TestCase):
    def test_missing_field_template_is_a_permanent_fail_closed_error(self) -> None:
        import pymupdf

        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Documento no compatible con la plantilla DIAN")
            source = document.tobytes()
        with self.assertRaises(PermanentProcessingError):
            redact_pdf(source)


if __name__ == "__main__":
    unittest.main()
