from __future__ import annotations

import unittest

import fitz

from api.documents.combine import _merge_pdf_contents


def make_pdf(page_count: int) -> bytes:
    document = fitz.open()
    try:
        for index in range(page_count):
            page = document.new_page()
            page.insert_text((72, 72), f"page-{index + 1}")
        return document.tobytes()
    finally:
        document.close()


def make_declaration(form_number: str, duplicate_pages: bool = False) -> bytes:
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), f"4. Número de formulario {form_number}")
        page = document.new_page()
        page.insert_text((72, 72), f"Aceptación: {form_number} continuación de mercancías")
        if duplicate_pages:
            page = document.new_page()
            page.insert_text((72, 72), f"4. Número de formulario {form_number}")
            page = document.new_page()
            page.insert_text((72, 72), f"Aceptación: {form_number} continuación de mercancías")
        return document.tobytes()
    finally:
        document.close()


class CombinePdfTests(unittest.TestCase):
    def test_merges_multiple_documents_in_order(self) -> None:
        combined = _merge_pdf_contents([make_pdf(2), make_pdf(1)])
        with fitz.open(stream=combined, filetype="pdf") as document:
            self.assertEqual(document.page_count, 3)
            self.assertIn("page-1", document[0].get_text())
            self.assertIn("page-2", document[1].get_text())
            self.assertIn("page-1", document[2].get_text())

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            _merge_pdf_contents([])

    def test_omits_duplicate_declaration_documents(self) -> None:
        declaration = make_declaration("032026001506262-1")
        combined = _merge_pdf_contents([declaration, declaration])
        with fitz.open(stream=combined, filetype="pdf") as document:
            self.assertEqual(document.page_count, 2)

    def test_removes_duplicate_pages_inside_one_declaration_pdf(self) -> None:
        declaration = make_declaration("032026001506262-1", duplicate_pages=True)
        combined = _merge_pdf_contents([declaration])
        with fitz.open(stream=combined, filetype="pdf") as document:
            self.assertEqual(document.page_count, 2)


if __name__ == "__main__":
    unittest.main()
