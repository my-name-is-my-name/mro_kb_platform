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
from unittest.mock import patch

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

    def test_native_contract_matches_dedicated_endpoint(self) -> None:
        status, payload = self.request(
            "/api/ata-impact",
            {
                "request": "damage",
                "aircraft_type": "A320",
                "fields": {"aircraft_type": "A320"},
                "mode": "standard",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(StubAgent.calls[-1][1], {"aircraft_type": "A320"})
        self.assertEqual(StubAgent.calls[-1][2], "standard")

    def test_conflict_and_invalid_mode_are_400(self) -> None:
        status, payload = self.request(
            "/api/ata-impact",
            {
                "request": "damage",
                "aircraft_type": "A320",
                "fields": {"aircraft_type": "B737"},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        status, payload = self.request(
            "/api/ata-impact",
            {"request": "damage", "mode": "standrd"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_openai_nonstream_and_sse_contracts(self) -> None:
        status, completion = self.request(
            "/v1/chat/completions",
            {
                "model": "mro-ata-impact",
                "mode": "extended",
                "messages": [{"role": "user", "content": "damage"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(completion["choices"][0]["message"]["content"], "ATA result")
        self.assertEqual(StubAgent.calls[-1][2], "extended")
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "mro-ata-impact",
                    "stream": True,
                    "messages": [{"role": "user", "content": "damage"}],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(
                response.headers.get_content_type(),
                "text/event-stream",
            )
            body = response.read().decode()
        self.assertIn('"finish_reason": "stop"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))


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

    def test_native_conflict_and_invalid_mode_parity_without_sockets(self) -> None:
        conflict = {
            "request": "damage",
            "aircraft_type": "A320",
            "fields": {"aircraft_type": "B737"},
        }
        invalid = {"request": "damage", "mode": "standrd"}
        for handler, sender in (
            (dedicated_server.AtaHandler, "_json"),
            (main_server.RequestHandler, "_send_json"),
        ):
            with self.subTest(handler=handler.__name__, case="conflict"):
                status, payload = self.invoke(handler, "/api/ata-impact", conflict, sender)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["type"], "invalid_request_error")
            with self.subTest(handler=handler.__name__, case="mode"):
                status, payload = self.invoke(handler, "/api/ata-impact", invalid, sender)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_native_request_alias_conflicts_have_identical_contract(self) -> None:
        conflict = {"request": "damage A", "question": "damage B"}
        identical = {"request": "same damage", "question": "same damage"}
        for handler, sender in (
            (dedicated_server.AtaHandler, "_json"),
            (main_server.RequestHandler, "_send_json"),
        ):
            with self.subTest(handler=handler.__name__, case="conflict"):
                status, payload = self.invoke(
                    handler,
                    "/api/ata-impact",
                    conflict,
                    sender,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["type"], "invalid_request_error")
            with self.subTest(handler=handler.__name__, case="identical"):
                status, _ = self.invoke(
                    handler,
                    "/api/ata-impact",
                    identical,
                    sender,
                )
                self.assertEqual(status, 200)
                self.assertEqual(StubAgent.calls[-1][0], "same damage")

    def test_openai_invalid_mode_and_enabled_legacy_without_sockets(self) -> None:
        chat = {
            "model": "mro-ata-impact",
            "mode": "AUTO",
            "messages": [{"role": "user", "content": "damage"}],
        }
        for handler, sender in (
            (dedicated_server.AtaHandler, "_json"),
            (main_server.RequestHandler, "_send_json"),
        ):
            status, payload = self.invoke(
                handler,
                "/v1/chat/completions",
                chat,
                sender,
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["type"], "invalid_request_error")
        with patch.dict(
            "os.environ",
            {"MRO_KB_ENABLE_LEGACY_ATA_MODES": "true"},
        ):
            for handler, sender in (
                (dedicated_server.AtaHandler, "_json"),
                (main_server.RequestHandler, "_send_json"),
            ):
                status, _ = self.invoke(
                    handler,
                    "/api/ata-impact",
                    {"request": "damage", "mode": "rules_only"},
                    sender,
                )
                self.assertEqual(status, 200)
                self.assertEqual(StubAgent.calls[-1][2], "rules_only")

        invalid_stream = {
            "model": "mro-ata-impact",
            "stream": "false",
            "messages": [{"role": "user", "content": "damage"}],
        }
        for handler, sender in (
            (dedicated_server.AtaHandler, "_json"),
            (main_server.RequestHandler, "_send_json"),
        ):
            status, payload = self.invoke(
                handler,
                "/v1/chat/completions",
                invalid_stream,
                sender,
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_agent_exceptions_are_json_500_on_both_endpoints(self) -> None:
        class BrokenAgent(StubAgent):
            def analyze(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise RuntimeError("boom")

        main_server.SERVICES = SimpleNamespace(ata_impact=BrokenAgent())  # type: ignore[assignment]
        dedicated_server.ATA_AGENT = BrokenAgent()  # type: ignore[assignment]
        for handler, sender in (
            (dedicated_server.AtaHandler, "_json"),
            (main_server.RequestHandler, "_send_json"),
        ):
            status, payload = self.invoke(
                handler,
                "/api/ata-impact",
                {"request": "damage"},
                sender,
            )
            self.assertEqual(status, 500)
            self.assertEqual(payload["error"]["type"], "server_error")

    def test_sse_envelopes_and_mode_forwarding_without_sockets(self) -> None:
        for handler_class, method_name in (
            (dedicated_server.AtaHandler, "_stream"),
            (main_server.RequestHandler, "_send_ata_impact_stream"),
        ):
            handler = object.__new__(handler_class)
            handler.wfile = io.BytesIO()
            headers: dict[str, str] = {}
            handler.send_response = MethodType(lambda self, status: None, handler)
            handler.send_header = MethodType(
                lambda self, key, value: headers.__setitem__(key, value),
                handler,
            )
            handler.end_headers = MethodType(lambda self: None, handler)
            handler.close_connection = False
            getattr(handler, method_name)("damage", "extended")
            body = handler.wfile.getvalue().decode()
            self.assertEqual(
                headers["Content-Type"],
                "text/event-stream; charset=utf-8",
            )
            self.assertIn('"finish_reason": "stop"', body)
            self.assertTrue(body.rstrip().endswith("data: [DONE]"))
            self.assertEqual(StubAgent.calls[-1][2], "extended")


if __name__ == "__main__":
    unittest.main()
