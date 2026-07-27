"""Small dedicated OpenAI-compatible server for the first ATA intake layer.

It deliberately avoids constructing the retrieval, similar-case and Go/No-Go
services.  This keeps the OpenWebUI endpoint available even while document
indexes are large or unavailable.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from core.config import SQLITE_PATH
from core.ata_impact.http_contract import (
    extract_ata_request_text,
    merge_ata_request_fields,
    validate_stream_flag,
)
from core.ata_impact.modes import validate_ata_runtime_mode
from core.go_no_go import AtaDiscoveryCatalog, AtaImpactAgent, CertificateCatalog, InternalEvidenceRetriever
from storage.sqlite.store import SQLiteStore


CERTIFICATE = CertificateCatalog()
ATA_STORE = SQLiteStore(SQLITE_PATH)
ATA_STORE.initialize()
ATA_AGENT = AtaImpactAgent(CERTIFICATE, AtaDiscoveryCatalog(CERTIFICATE), retriever=InternalEvidenceRetriever(ATA_STORE))


class AtaHandler(BaseHTTPRequestHandler):
    server_version = "MROATAImpact/1.1"

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            return self._json({"status": "ok", "service": "mro-ata-impact", **ATA_AGENT.health()})
        if path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": "mro-ata-impact", "object": "model", "created": int(time.time()), "owned_by": "mro-kb-platform"}]})
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": {"message": "Invalid JSON", "type": "invalid_request_error"}}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": {"message": "JSON body must be an object", "type": "invalid_request_error"}}, 400)
        path = urlparse(self.path).path
        if path == "/api/ata-impact":
            try:
                question = extract_ata_request_text(payload)
                if not question:
                    return self._json({"ok": False, "error": "request is required"}, 400)
                fields = merge_ata_request_fields(payload)
                mode = validate_ata_runtime_mode(
                    payload["mode"] if "mode" in payload else "auto",
                    allow_legacy=True,
                )
                result = ATA_AGENT.analyze(question, fields, mode=mode)
            except ValueError as exc:
                return self._json(
                    {"error": {"message": str(exc), "type": "invalid_request_error"}},
                    400,
                )
            except Exception:
                return self._json(
                    {"error": {"message": "ATA analysis failed", "type": "server_error"}},
                    500,
                )
            return self._json({"ok": True, "ata_impact": result})
        if path != "/v1/chat/completions":
            return self._json({"error": "not found"}, 404)
        if str(payload.get("model") or "") != "mro-ata-impact":
            return self._json({"error": {"message": "Unknown model", "type": "invalid_request_error"}}, 404)
        messages = payload.get("messages") or []
        question = next((str(item.get("content") or "").strip() for item in reversed(messages) if isinstance(item, dict) and item.get("role") == "user" and str(item.get("content") or "").strip()), "")
        if not question:
            return self._json({"error": {"message": "User message is required", "type": "invalid_request_error"}}, 400)
        try:
            mode = validate_ata_runtime_mode(
                payload["mode"] if "mode" in payload else "auto",
                allow_legacy=True,
            )
            stream = validate_stream_flag(payload)
        except ValueError as exc:
            return self._json(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                400,
            )
        if not stream:
            try:
                result = ATA_AGENT.analyze(question, mode=mode)
            except Exception:
                return self._json(
                    {"error": {"message": "ATA analysis failed", "type": "server_error"}},
                    500,
                )
            return self._json({"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()), "model": "mro-ata-impact", "choices": [{"index": 0, "message": {"role": "assistant", "content": result["answer"]}, "finish_reason": "stop"}], "ata_impact": result})
        self._stream(question, mode)

    def _stream(self, question: str, mode: str = "auto") -> None:
        completion_id, events, result_box = f"chatcmpl-{uuid.uuid4().hex}", queue.Queue(), {}
        def work() -> None:
            try:
                result_box["result"] = ATA_AGENT.analyze(question, progress=events.put, mode=mode)
            except Exception:
                result_box["error"] = True
            finally:
                events.put({"stage": "_finished"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        def emit(delta: dict[str, object], finish: str | None = None) -> None:
            item = {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": "mro-ata-impact", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8")); self.wfile.flush()
        emit({"role": "assistant"})
        threading.Thread(target=work, daemon=True).start()
        while True:
            event = events.get()
            if event.get("stage") == "_finished": break
            if event.get("message"): emit({"reasoning_content": str(event["message"]) + "\n"})
        content = "Не удалось завершить предварительную оценку ATA." if result_box.get("error") else str(result_box["result"]["answer"])
        emit({"content": content}); emit({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="0.0.0.0"); parser.add_argument("--port", type=int, default=8122)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), AtaHandler).serve_forever()


if __name__ == "__main__": main()
