from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import apps.api.ata_server as ata_server


class StubAgent:
    def health(self) -> dict[str, object]:
        return {"pipeline_version": "v2", "default_mode": "auto"}

    def analyze(self, request: str, fields: dict[str, object] | None = None, progress: object | None = None, mode: str = "auto") -> dict[str, object]:
        if callable(progress):
            progress({"stage": "completed", "message": "done"})
        return {
            "answer": "ATA result",
            "affected_ata": ["ATA 25"],
            "potentially_affected_ata": [],
            "context_ata": [],
            "needs_human_approval": True,
            "decision": "completed_with_hypotheses",
        }


class AtaHttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ata_server.ATA_AGENT = StubAgent()  # type: ignore[assignment]
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ata_server.AtaHandler)
        except PermissionError as exc:
            raise unittest.SkipTest("local sockets are blocked by the test sandbox") from exc
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path: str, payload: object | None = None) -> tuple[int, dict[str, object]]:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_native_ata_wrapper_and_default_mode(self) -> None:
        status, payload = self.request("/api/ata-impact", {"request": "damage"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["ata_impact"]["affected_ata"], ["ATA 25"])

    def test_openai_models_and_nonstream_envelope(self) -> None:
        status, models = self.request("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["data"][0]["id"], "mro-ata-impact")
        status, completion = self.request(
            "/v1/chat/completions",
            {"model": "mro-ata-impact", "messages": [{"role": "user", "content": "damage"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(completion["choices"][0]["message"]["content"], "ATA result")
        self.assertEqual(completion["ata_impact"]["affected_ata"], ["ATA 25"])

    def test_stream_envelope_finishes_with_done(self) -> None:
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "mro-ata-impact", "stream": True, "messages": [{"role": "user", "content": "damage"}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()
        self.assertIn('"reasoning_content": "done\\n"', body)
        self.assertIn('"content": "ATA result"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))

    def test_invalid_json_shapes_and_unknown_model_are_4xx(self) -> None:
        status, payload = self.request("/api/ata-impact", [])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        status, payload = self.request(
            "/v1/chat/completions",
            {"model": "unknown", "messages": [{"role": "user", "content": "damage"}]},
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
