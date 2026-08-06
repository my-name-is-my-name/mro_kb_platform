from __future__ import annotations

import json
import io
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from types import MethodType

import apps.api.ata_server as dedicated_server
import apps.api.server as main_server
from tests.test_ata_http_contract import StubAgent


class MainAtaHttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_services = main_server.SERVICES
        main_server.SERVICES = SimpleNamespace(ata_impact=StubAgent())  # type: ignore[assignment]
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), main_server.RequestHandler)
        except PermissionError as exc:
            main_server.SERVICES = cls.original_services
            raise unittest.SkipTest("local sockets are blocked by the test sandbox") from exc
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
        main_server.SERVICES = cls.original_services

    def setUp(self) -> None:
        StubAgent.calls.clear()

    def request(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_native_ata_endpoint_is_disabled_on_main_api(self) -> None:
        status, payload = self.request(
            "/api/ata-impact",
            {
                "request": "damage",
                "aircraft_type": "A320",
                "fields": {"aircraft_type": "A320"},
                "mode": "standard",
            },
        )
        self.assertEqual(status, 410)
        self.assertFalse(payload["ok"])
        self.assertIn("8122", payload["error"])
        self.assertEqual(StubAgent.calls, [])

    def test_main_models_do_not_publish_ata_impact(self) -> None:
        with urllib.request.urlopen(self.base + "/v1/models", timeout=5) as response:
            models = json.loads(response.read())
        ids = {item["id"] for item in models["data"]}
        self.assertNotIn("mro-ata-impact", ids)
        self.assertNotIn("mro-go-no-go", ids)

    def test_openai_ata_model_is_unknown_on_main_api(self) -> None:
        status, payload = self.request(
            "/v1/chat/completions",
            {
                "model": "mro-ata-impact",
                "messages": [{"role": "user", "content": "damage"}],
            },
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_openai_go_no_go_model_is_unknown_on_main_api(self) -> None:
        status, payload = self.request(
            "/v1/chat/completions",
            {
                "model": "mro-go-no-go",
                "messages": [{"role": "user", "content": "damage"}],
            },
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")


class SocketlessAtaHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        StubAgent.calls.clear()
        self.original_main_services = main_server.SERVICES
        self.original_dedicated_agent = dedicated_server.ATA_AGENT
        main_server.SERVICES = SimpleNamespace(ata_impact=StubAgent())  # type: ignore[assignment]
        dedicated_server.ATA_AGENT = StubAgent()  # type: ignore[assignment]

    def tearDown(self) -> None:
        main_server.SERVICES = self.original_main_services
        dedicated_server.ATA_AGENT = self.original_dedicated_agent

    def invoke(
        self,
        handler_class: type,
        path: str,
        payload: dict[str, object],
        sender_name: str,
    ) -> tuple[int, dict[str, object]]:
        raw = json.dumps(payload).encode()
        handler = object.__new__(handler_class)
        handler.path = path
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        captured: dict[str, object] = {}

        def capture(
            instance: object,
            response_payload: dict[str, object],
            status: int = 200,
        ) -> None:
            captured["status"] = status
            captured["payload"] = response_payload

        setattr(handler, sender_name, MethodType(capture, handler))
        handler.do_POST()
        return int(captured["status"]), captured["payload"]  # type: ignore[return-value]

    def test_dedicated_native_conflict_and_invalid_mode_without_sockets(self) -> None:
        conflict = {
            "request": "damage",
            "aircraft_type": "A320",
            "fields": {"aircraft_type": "B737"},
        }
        invalid = {"request": "damage", "mode": "standrd"}
        status, payload = self.invoke(dedicated_server.AtaHandler, "/api/ata-impact", conflict, "_json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        status, payload = self.invoke(dedicated_server.AtaHandler, "/api/ata-impact", invalid, "_json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_dedicated_request_alias_conflicts_have_identical_contract(self) -> None:
        conflict = {"request": "damage A", "question": "damage B"}
        identical = {"request": "same damage", "question": "same damage"}
        status, payload = self.invoke(dedicated_server.AtaHandler, "/api/ata-impact", conflict, "_json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        status, _ = self.invoke(dedicated_server.AtaHandler, "/api/ata-impact", identical, "_json")
        self.assertEqual(status, 200)
        self.assertEqual(StubAgent.calls[-1][0], "same damage")

    def test_dedicated_openai_invalid_mode_without_sockets(self) -> None:
        chat = {
            "model": "mro-ata-impact",
            "mode": "AUTO",
            "messages": [{"role": "user", "content": "damage"}],
        }
        status, payload = self.invoke(dedicated_server.AtaHandler, "/v1/chat/completions", chat, "_json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

        invalid_stream = {
            "model": "mro-ata-impact",
            "stream": "false",
            "messages": [{"role": "user", "content": "damage"}],
        }
        status, payload = self.invoke(dedicated_server.AtaHandler, "/v1/chat/completions", invalid_stream, "_json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_dedicated_agent_exceptions_are_json_500(self) -> None:
        class BrokenAgent(StubAgent):
            def analyze(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise RuntimeError("boom")

        main_server.SERVICES = SimpleNamespace(ata_impact=BrokenAgent())  # type: ignore[assignment]
        dedicated_server.ATA_AGENT = BrokenAgent()  # type: ignore[assignment]
        status, payload = self.invoke(dedicated_server.AtaHandler, "/api/ata-impact", {"request": "damage"}, "_json")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["type"], "server_error")

    def test_main_handler_rejects_ata_impact_without_agent_call(self) -> None:
        status, payload = self.invoke(main_server.RequestHandler, "/api/ata-impact", {"request": "damage"}, "_send_json")
        self.assertEqual(status, 410)
        self.assertIn("8122", payload["error"])
        self.assertEqual(StubAgent.calls, [])

        status, payload = self.invoke(
            main_server.RequestHandler,
            "/v1/chat/completions",
            {"model": "mro-ata-impact", "messages": [{"role": "user", "content": "damage"}]},
            "_send_json",
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_main_openwebui_similar_cases_does_not_emit_sources(self) -> None:
        class FakeCommercialOffers:
            def similar_cases(self, question: str) -> dict[str, object]:
                return {
                    "answer": "| Заявка | Score |\n|---|---:|\n| MP-1 | 1.0 |",
                    "sources": [{"title": "hidden document"}],
                    "warnings": [],
                }

        main_server.SERVICES = SimpleNamespace(commercial_offers=FakeCommercialOffers())  # type: ignore[assignment]
        status, payload = self.invoke(
            main_server.RequestHandler,
            "/v1/chat/completions",
            {"model": "mro-similar-cases", "messages": [{"role": "user", "content": "frame crack"}]},
            "_send_json",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["sources"], [])
        self.assertIn("MP-1", payload["choices"][0]["message"]["content"])

    def test_main_similar_cases_search_endpoint_is_machine_contract(self) -> None:
        class FakeCommercialOffers:
            def search_similar_cases(self, payload: dict[str, object]) -> dict[str, object]:
                return {
                    "status": "ok",
                    "similarity_status": "qualified_matches_found",
                    "threshold_version": "similarity-gate-v1",
                    "accepted": [{"case_id": "A1"}],
                    "not_accepted": [{"case_id": "R1"}],
                    "coverage": {"accepted_available": 1, "not_accepted_available": 1, "unknown_status_excluded": 0},
                    "warnings": [],
                    "echo_context": payload.get("context"),
                }

        main_server.SERVICES = SimpleNamespace(commercial_offers=FakeCommercialOffers())  # type: ignore[assignment]
        status, payload = self.invoke(
            main_server.RequestHandler,
            "/api/similar-cases/search",
            {"query": "frame crack", "context": {"ata": ["ATA 53"]}},
            "_send_json",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["accepted"][0]["case_id"], "A1")
        self.assertEqual(payload["not_accepted"][0]["case_id"], "R1")
        self.assertEqual(payload["echo_context"], {"ata": ["ATA 53"]})

    def test_sse_envelopes_and_mode_forwarding_without_sockets(self) -> None:
        handler = object.__new__(dedicated_server.AtaHandler)
        handler.wfile = io.BytesIO()
        headers: dict[str, str] = {}
        handler.send_response = MethodType(lambda self, status: None, handler)
        handler.send_header = MethodType(lambda self, key, value: headers.__setitem__(key, value), handler)
        handler.end_headers = MethodType(lambda self: None, handler)
        handler.close_connection = False
        handler._stream("damage", "extended")
        body = handler.wfile.getvalue().decode()
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertIn('"finish_reason": "stop"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))
        self.assertEqual(StubAgent.calls[-1][2], "extended")


if __name__ == "__main__":
    unittest.main()
