from __future__ import annotations

import unittest
from unittest.mock import patch
import pandas as pd
import pymupdf

from app.graph import GraphClient
from app.errors import GraphAccessDeniedError, PermanentProcessingError
from app.pdf_service import redact_pdf
from app.pdf_splitter import split_declarations_with_report
from app.security import create_jwt, decode_jwt
from app.service import (
    _excel_columns,
    _extract_declaration,
    _filename_matches_form,
    _find_pdf,
    _pdf_contains_form,
    _search_container,
    _safe_graph_search,
    _sheet_excel_columns,
    _walk_scope_for_containers,
)
from api.admin.users import _parse_active, handler as AdminUsersHandler
from api.auth.login import _normalize_login_role
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

    def test_filename_match_tolerates_one_digit_error_only_when_unambiguous(self) -> None:
        item = {"id": "one", "name": "032026001406315-1.pdf", "file": {}}
        self.assertTrue(_filename_matches_form(item["name"], "032026001406316-1"))
        self.assertEqual(_find_pdf([item], "032026001406316-1"), item)
        duplicate = {"id": "two", "name": "032026001406317-1.pdf", "file": {}}
        self.assertIsNone(_find_pdf([item, duplicate], "032026001406316-1"))

    def test_extractor_stops_before_the_next_declaration_and_keeps_continuation(self) -> None:
        with pymupdf.open() as source:
            first = source.new_page()
            first.insert_text((50, 50), "4. Número de formulario\n032026001506542-7")
            continuation = source.new_page()
            continuation.insert_text((50, 50), "Aceptación : 032026001506542-7\nPágina 1 de 1")
            next_declaration = source.new_page()
            next_declaration.insert_text((50, 50), "4. Número de formulario\n032026001506262-1")
            source_bytes = source.tobytes()

        extracted = _extract_declaration(source_bytes, "032026001506542-7")
        with pymupdf.open(stream=extracted, filetype="pdf") as result:
            self.assertEqual(len(result), 2)
            self.assertTrue(_pdf_contains_form(extracted, "032026001506542-7"))
            self.assertFalse(_pdf_contains_form(extracted, "032026001506262-1"))

    def test_supplied_pdf_sample_extracts_only_form_542_and_its_continuation(self) -> None:
        from pathlib import Path

        sample = Path(r"C:\Users\juanc\Downloads\032026001506542-7.pdf")
        if not sample.exists():
            self.skipTest("User PDF attachment is not present on this test machine")
        original = sample.read_bytes()
        self.assertFalse(_pdf_contains_form(original, "032026001506542-7"))
        extracted = _extract_declaration(original, "032026001506542-7")
        with pymupdf.open(stream=extracted, filetype="pdf") as result:
            self.assertEqual(len(result), 2)
            self.assertTrue(_pdf_contains_form(extracted, "032026001506542-7"))
            self.assertFalse(_pdf_contains_form(extracted, "032026001506262-1"))

    def test_splitter_separates_supplied_sample_into_exact_declarations(self) -> None:
        from pathlib import Path

        sample = Path(r"C:\Users\juanc\Downloads\032026001506542-7.pdf")
        if not sample.exists():
            self.skipTest("User PDF attachment is not present on this test machine")
        files, skipped = split_declarations_with_report(sample.read_bytes())
        self.assertEqual(skipped, [])
        self.assertEqual(
            [name for name, _ in files],
            ["032026001506542-7.pdf", "032026001506262-1.pdf"],
        )
        page_counts = {}
        for name, content in files:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                page_counts[name] = len(document)
        self.assertEqual(page_counts["032026001506542-7.pdf"], 2)
        self.assertEqual(page_counts["032026001506262-1.pdf"], 1)

    def test_splitter_skips_continuation_with_conflicting_acceptance_id(self) -> None:
        with pymupdf.open() as source:
            first = source.new_page()
            first.insert_text((50, 50), "4. Número de formulario\n032026001506542-7")
            conflicting_continuation = source.new_page()
            conflicting_continuation.insert_text((50, 50), "Aceptación: 032026001506262-1\nPágina 1 de 1")
            second = source.new_page()
            second.insert_text((50, 50), "4. Número de formulario\n032026001506262-1")
            source_bytes = source.tobytes()

        files, skipped = split_declarations_with_report(source_bytes)
        self.assertEqual(skipped, [2])
        self.assertEqual(
            [name for name, _ in files],
            ["032026001506542-7.pdf", "032026001506262-1.pdf"],
        )
        self.assertTrue(all(len(pymupdf.open(stream=content, filetype="pdf")) == 1 for _, content in files))

    def test_search_checks_processed_before_container_tree_and_validates_pdf(self) -> None:
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "4. Número de formulario\n032026001506542-7")
            valid_pdf = document.tobytes()

        class FakeGraph:
            base = "https://graph.microsoft.com/v1.0"
            drive_base = f"{base}/drives/test-drive"

            def __init__(self) -> None:
                self.listed: list[str] = []

            def get_item_by_path(self, _path: str) -> dict[str, object]:
                return {"id": "container", "folder": {}}

            def list_item_children(self, item_id: str) -> list[dict[str, object]]:
                self.listed.append(item_id)
                if item_id == "container":
                    return [
                        {"id": "processed", "name": "Procesados", "folder": {}},
                        {"id": "dim", "name": "DIM", "folder": {}},
                    ]
                if item_id == "processed":
                    return [{
                        "id": "decl",
                        "name": "032026001506542-7.pdf",
                        "file": {},
                        "parentReference": {"driveId": "test-drive"},
                    }]
                raise AssertionError("The search should return from Procesados before listing DIM")

            def download_item(self, _item: dict[str, object]) -> bytes:
                return valid_pdf

        graph = FakeGraph()
        located, consolidations = _search_container(
            graph, "2026/Contenedor 147", "032026001506542-7", []
        )
        self.assertIsNotNone(located)
        self.assertEqual(located.parent_folder, "2026/Contenedor 147/Procesados")
        self.assertEqual(consolidations, [])
        self.assertEqual(graph.listed, ["container", "processed"])

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

    def test_drive_item_search_is_scoped_to_dim_root_folder(self) -> None:
        graph = object.__new__(GraphClient)
        graph.base = "https://graph.microsoft.com/v1.0"
        graph.drive_base = f"{graph.base}/users/owner@example.com/drive"
        requested: list[str] = []

        def fake_request(method: str, url: str, **kwargs: object) -> object:
            requested.append(url)
            return type("Response", (), {"json": lambda self: {"value": []}})()

        graph._request = fake_request  # type: ignore[method-assign]
        self.assertEqual(graph.search_items("A367-LLA-81", folder_item_id="dim-root-id"), [])
        self.assertIn("/items/dim-root-id/search(q='A367-LLA-81')", requested[0])
        self.assertNotIn("/root/search", requested[0])

    def test_graph_search_errors_are_not_converted_to_empty_results(self) -> None:
        graph = type("SearchGraph", (), {"search_items": lambda self, query, **kwargs: (_ for _ in ()).throw(RuntimeError("Graph unavailable"))})()
        with self.assertRaisesRegex(RuntimeError, "Graph unavailable"):
            _safe_graph_search(graph, "reference", "dim-root-id")

    def test_denied_global_graph_search_falls_back_to_local_container_search(self) -> None:
        graph = type("SearchGraph", (), {"search_items": lambda self, query, **kwargs: (_ for _ in ()).throw(GraphAccessDeniedError("HTTP 403 accessDenied"))})()
        self.assertEqual(_safe_graph_search(graph, "032026001506542-7", "dim-root-id"), [])

    def test_container_fallback_crawls_only_the_configured_root_hierarchy(self) -> None:
        tree = {
            "dim-root": [
                {"id": "year-2026", "name": "2026", "folder": {}},
                {"id": "other", "name": "Otros", "folder": {}},
            ],
            "year-2026": [
                {"id": "container-147", "name": "Contenedor 147 - IMP2026-148", "folder": {}},
            ],
            "other": [],
            "container-147": [],
        }
        graph = type("FolderGraph", (), {
            "list_item_children": lambda self, item_id: tree.get(item_id, []),
        })()
        diagnostics: list[str] = []
        matches = _walk_scope_for_containers(
            graph, "dim-root", "Impromarkas/Facturas", "Cont. 147", diagnostics
        )
        self.assertEqual(matches, ["Impromarkas/Facturas/2026/Contenedor 147 - IMP2026-148"])

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
    def test_login_role_is_normalized_for_wix_route_guards(self) -> None:
        self.assertEqual(_normalize_login_role(" Client "), "client")
        self.assertEqual(_normalize_login_role("ADMIN"), "admin")
        self.assertIsNone(_normalize_login_role("customer"))

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

    def test_admin_users_route_exposes_password_change_method(self) -> None:
        self.assertTrue(callable(getattr(AdminUsersHandler, "do_PATCH", None)))

    def test_password_change_validates_length_and_uses_client_only(self) -> None:
        from app.users import change_client_password

        with self.assertRaises(ValueError):
            change_client_password("client-id", "short")

        from unittest.mock import MagicMock, patch

        connection = MagicMock()
        cursor = MagicMock()
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = {"id": "client-id", "role": "client"}
        cursor.rowcount = 1
        with patch("app.users.ensure_schema"), \
             patch("app.users._connection_pool", return_value=pool), \
             patch("app.users.hash_password", return_value="hashed") as hash_password:
            self.assertTrue(change_client_password("client-id", "long-enough-password"))
            hash_password.assert_called_once_with("long-enough-password")
            self.assertIn("LOWER(BTRIM(role)) = 'client'", cursor.execute.call_args_list[1].args[0])

        cursor.reset_mock()
        cursor.fetchone.return_value = {"id": "admin-id", "role": "admin"}
        with patch("app.users.ensure_schema"), \
             patch("app.users._connection_pool", return_value=pool), \
             patch("app.users.hash_password") as hash_password:
            with self.assertRaises(PermissionError):
                change_client_password("admin-id", "long-enough-password")
            hash_password.assert_not_called()


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
