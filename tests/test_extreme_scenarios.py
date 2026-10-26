from __future__ import annotations

import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymupdf

from app.graph import GraphClient
from app.pdf_service import redact_pdf
from app.pdf_splitter import split_declarations_with_report


def _form_number(index: int) -> str:
    return f"032026{index:09d}-{index % 10}"


class ExtremePdfTests(unittest.TestCase):
    def test_splitter_handles_210_pages_and_keeps_each_four_page_declaration(self) -> None:
        source = pymupdf.open()
        expected = []
        try:
            for index in range(1, 51):
                form = _form_number(index)
                expected.append(form)
                first = source.new_page()
                first.insert_text((40, 50), f"4. Número de formulario\n{form}")
                for page_number in range(2, 5):
                    continuation = source.new_page()
                    continuation.insert_text((40, 50), f"Aceptación: {form} página {page_number}")
                if index % 5 == 0:
                    source.new_page()  # blank-page stress case
            source_bytes = source.tobytes(garbage=4, deflate=True)
        finally:
            source.close()

        with patch("app.pdf_splitter.logger.info"):
            files, skipped = split_declarations_with_report(source_bytes)
        self.assertEqual(len(files), 50)
        self.assertEqual(len(skipped), 10)
        self.assertEqual([name for name, _ in files], [f"{form}.pdf" for form in expected])
        for _name, pdf_bytes in files:
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
                self.assertEqual(document.page_count, 4)

    def test_redaction_removes_field_values_optional_term_and_document_metadata(self) -> None:
        from app.pdf_service import _FIELD_LABELS, _SENSITIVE_FIELD_RECTS

        source = pymupdf.open()
        page = source.new_page(width=622, height=890)
        labels = {
            46: "46 nombre exportador o proveedor en el exterior",
            47: "47 ciudad",
            49: "49 direccion exportador o proveedor en el exterior",
            50: "50 e mail",
            78: "78 valor fob usd",
            80: "80 valor seguros usd",
            82: "82 sumatoria de fletes seguros y otros gastos usd",
            84: "84 valor aduana usd",
        }
        values = {}
        for offset, (field, label) in enumerate(labels.items()):
            page.insert_text((20, 80 + offset * 18), label, fontsize=8)
            x0, y0, x1, y1 = _SENSITIVE_FIELD_RECTS[field]
            value = f"SECRET_FIELD_{field}"
            page.insert_text((x0 + 2, y1 - 1), value, fontsize=6)
            values[field] = value
        optional_term = "OPTIONAL_SECRET_VALUE"
        page.insert_text((520, 800), optional_term, fontsize=8)
        source.set_metadata({"title": "private title", "author": "private author", "subject": "sensitive"})
        source_bytes = source.tobytes()
        source.close()

        redacted = redact_pdf(source_bytes, [optional_term])
        with pymupdf.open(stream=redacted, filetype="pdf") as result:
            all_text = "\n".join(page.get_text() for page in result)
            for value in values.values():
                self.assertNotIn(value, all_text)
            self.assertNotIn(optional_term, all_text)
            self.assertFalse(any(
                value for key, value in (result.metadata or {}).items()
                if key not in {"format", "encryption"}
            ))
            self.assertEqual(result.xref_xml_metadata(), 0)

    def test_invalid_and_encoded_path_traversal_are_rejected(self) -> None:
        for path in ("../secret.pdf", "%2e%2e/secret.pdf", "safe/%2E%2E/secret.pdf"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                GraphClient._safe_path(path)


class ExtremeInvoiceTests(unittest.TestCase):
    def test_invoice_reference_reader_accepts_1000_and_rejects_1001(self) -> None:
        from app.invoice import MAX_INVOICE_REFERENCES, _read_sales_references
        import pandas as pd

        self.assertEqual(MAX_INVOICE_REFERENCES, 1000)
        workbook = MagicMock()
        workbook.__enter__.return_value = workbook
        workbook.sheet_names = ["BD"]

        def frame_with_count(count):
            return pd.DataFrame([
                ("INV-EXTREME", f"REF-{index:04d}") for index in range(count)
            ])

        with patch("app.invoice.pd.ExcelFile", return_value=workbook), \
             patch("app.invoice.pd.read_excel", return_value=frame_with_count(1000)):
            references = _read_sales_references(b"fixture", "INV-EXTREME")
        self.assertEqual(len(references), 1000)
        self.assertEqual(len(set(references)), 1000)

        with patch("app.invoice.pd.ExcelFile", return_value=workbook), \
             patch("app.invoice.pd.read_excel", return_value=frame_with_count(1001)):
            with self.assertRaisesRegex(ValueError, "1000"):
                _read_sales_references(b"fixture", "INV-EXTREME")


class ExtremeApiAndQueueTests(unittest.TestCase):
    @staticmethod
    def _request(handler_type, body: bytes, headers=None):
        request = object.__new__(handler_type)
        request.headers = {"Content-Length": str(len(body)), **(headers or {})}
        request.rfile = io.BytesIO(body)
        request.claims = lambda **_kwargs: {"sub": "client@example.com", "role": "client"}
        request.respond = MagicMock()
        return request

    def test_individual_reference_endpoint_rejects_more_than_30_before_writing_job(self) -> None:
        from api.dim.search_redact import handler as ReferenceHandler

        body = __import__("json").dumps({"references": [f"REF-{i}" for i in range(31)]}).encode()
        request = self._request(ReferenceHandler, body)
        with patch("api.dim.search_redact.create_job") as create_job:
            request.do_POST()
        self.assertEqual(request.respond.call_args.args[0], 400)
        self.assertFalse(create_job.called)

    def test_individual_reference_endpoint_accepts_exactly_30_references(self) -> None:
        from api.dim.search_redact import handler as ReferenceHandler

        references = [f"REF-{i}" for i in range(30)]
        body = __import__("json").dumps({"references": references}).encode()
        request = self._request(ReferenceHandler, body)
        with patch("api.dim.search_redact.create_job", return_value=("reference-job", True)) as create_job, \
             patch("api.dim.search_redact.enqueue_job", return_value=1), \
             patch("api.dim.search_redact.get_job", return_value={
                 "jobId": "reference-job", "status": "queued", "total": 30, "completed": 0,
             }):
            request.do_POST()
        self.assertEqual(request.respond.call_args.args[0], 202)
        self.assertEqual(len(create_job.call_args.args[1]), 30)

    def test_split_endpoint_creates_durable_admin_job(self) -> None:
        from api.admin.split_pdf import handler as SplitHandler

        body = b'{"filePath":"https://tenant.sharepoint.com/sites/docs/source.pdf"}'
        request = self._request(SplitHandler, body, {"Idempotency-Key": "split-extreme-1"})
        request.claims = lambda **_kwargs: {"sub": "admin@example.com", "role": "admin"}
        with patch("api.admin.split_pdf.create_job", return_value=("split-job", True)) as create_job, \
             patch("api.admin.split_pdf.enqueue_job", return_value=1), \
             patch("api.admin.split_pdf.get_job", return_value={"status": "queued"}):
            request.do_POST()
        self.assertEqual(request.respond.call_args.args[0], 202)
        self.assertEqual(create_job.call_args.kwargs["job_type"], "split_pdf")
        self.assertEqual(create_job.call_args.kwargs["job_metadata"]["file_path"],
                         "https://tenant.sharepoint.com/sites/docs/source.pdf")

    def test_login_endpoint_handles_success_and_rate_limit_without_echoing_password(self) -> None:
        from api.auth.login import handler as LoginHandler
        from app.security import decode_jwt

        secret = "test-only-secret-that-is-longer-than-thirty-two-bytes"
        password = "correct-horse-battery-staple"
        body = __import__("json").dumps({"email": "CLIENT@example.com", "password": password}).encode()
        request = self._request(LoginHandler, body)
        with patch.dict("os.environ", {"JWT_SECRET": secret}), \
             patch("api.auth.login.login_is_rate_limited", return_value=False), \
             patch("api.auth.login.authenticate", return_value={"email": "client@example.com", "role": "Client"}), \
             patch("api.auth.login.clear_login_failures"):
            request.do_POST()
        status, payload = request.respond.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "client")
        self.assertNotIn(password, str(payload))
        self.assertEqual(decode_jwt(payload["token"], secret)["role"], "client")

        limited = self._request(LoginHandler, body)
        with patch("api.auth.login.login_is_rate_limited", return_value=True), \
             patch("api.auth.login.authenticate") as authenticate:
            limited.do_POST()
        self.assertEqual(limited.respond.call_args.args[0], 429)
        authenticate.assert_not_called()

    def test_invoice_endpoint_rejects_invalid_and_oversized_invoice_values(self) -> None:
        from api.dim.process_invoice import handler as InvoiceHandler

        invalid_bodies = [
            b'{"invoice":""}',
            b'{"invoice":"INV#1"}',
            (b'{"invoice":"' + b"A" * 121 + b'"}'),
        ]
        for body in invalid_bodies:
            request = self._request(InvoiceHandler, body)
            with self.subTest(body_length=len(body)), \
                 patch("api.dim.process_invoice.create_job") as create_job:
                request.do_POST()
                self.assertEqual(request.respond.call_args.args[0], 400)
                self.assertFalse(create_job.called)

    def test_queue_retries_transient_failure_then_stops_at_delivery_limit(self) -> None:
        from app.queue_worker import MAX_DELIVERIES, process_dim_reference
        from vercel.queue import RetryAfter

        message = SimpleNamespace(
            payload={"job_id": "job-uuid", "item_index": 0},
            metadata=SimpleNamespace(delivery_count=1),
        )
        claimed = {"reference": "REF-1", "job_type": "redact", "sensitive_terms": []}
        with patch("app.queue_worker.claim_item", return_value=claimed), \
             patch("app.queue_worker.Settings.from_env", return_value=object()), \
             patch("app.queue_worker.process_hierarchical_references", side_effect=RuntimeError("temporary Graph failure")), \
             patch("app.queue_worker.retry_or_fail_item", return_value=False) as retry_item:
            with self.assertRaises(RetryAfter) as raised:
                asyncio.run(process_dim_reference(message))
        self.assertEqual(raised.exception.timeout_seconds, 15)
        self.assertFalse(retry_item.call_args.kwargs["terminal"])

        message.metadata.delivery_count = MAX_DELIVERIES
        with patch("app.queue_worker.claim_item", return_value=claimed), \
             patch("app.queue_worker.Settings.from_env", return_value=object()), \
             patch("app.queue_worker.process_hierarchical_references", side_effect=RuntimeError("permanent fixture failure")), \
             patch("app.queue_worker.retry_or_fail_item", return_value=False) as retry_item, \
             patch("app.queue_worker._alert_terminal_failure") as alert:
            asyncio.run(process_dim_reference(message))
        self.assertTrue(retry_item.call_args.kwargs["terminal"])
        alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
