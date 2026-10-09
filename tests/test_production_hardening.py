from __future__ import annotations

import unittest

from app.graph import GraphClient
from app.security import create_jwt, decode_jwt
from app.service import _safe_graph_search
from api.admin.users import handler as UsersHandler


class GraphSearchTests(unittest.TestCase):
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
        self.assertFalse(UsersHandler._parse_active("false"))
        self.assertTrue(UsersHandler._parse_active("true"))
        with self.assertRaises(ValueError):
            UsersHandler._parse_active("False maybe")


if __name__ == "__main__":
    unittest.main()
