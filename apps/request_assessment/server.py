from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from core.request_assessment.models import AssessmentState
from core.request_assessment.progress import ProgressEvent, event_to_reasoning
from core.request_assessment.response_builder import build_final_content
from core.request_assessment.service import RequestAssessmentService


SERVICE = RequestAssessmentService()


class RequestAssessmentHandler(BaseHTTPRequestHandler):
    server_version = "MRORequestAssessment/0.1"

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(SERVICE.health())
        if path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": "mro-request-assessment", "object": "model", "created": int(time.time()), "owned_by": "mro-kb-platform"}]})
        if path.startswith("/api/assessments/"):
            request_id = path.rsplit("/", 1)[-1]
            state = SERVICE.get_assessment(request_id)
            if not state:
                return self._json({"ok": False, "error": "assessment not found"}, 404)
            return self._json({"ok": True, "assessment": state.model_dump(mode="json")})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            payload = self._read_json()
            if path == "/api/assessments":
                events: list[ProgressEvent] = []
                state = SERVICE.create_assessment(payload, events.append)
                return self._json({"ok": True, "assessment": state.model_dump(mode="json"), "progress_events": [item.model_dump(mode="json") for item in events]})
            if path.endswith("/answers") and path.startswith("/api/assessments/"):
                request_id = path.removeprefix("/api/assessments/").removesuffix("/answers").strip("/")
                events = []
                state = SERVICE.answer_questions(request_id, payload, events.append)
                return self._json({"ok": True, "assessment": state.model_dump(mode="json"), "progress_events": [item.model_dump(mode="json") for item in events]})
            if path.endswith("/review") and path.startswith("/api/assessments/"):
                request_id = path.removeprefix("/api/assessments/").removesuffix("/review").strip("/")
                state = SERVICE.human_review(request_id, payload)
                return self._json({"ok": True, "assessment": state.model_dump(mode="json")})
            if path == "/v1/chat/completions":
                return self._chat_completions(payload)
            return self._json({"error": "not found"}, 404)
        except KeyError:
            return self._json({"ok": False, "error": "assessment not found"}, 404)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return self._json({"error": {"message": str(exc), "type": "invalid_request_error"}}, 400)
        except Exception:
            return self._json({"error": {"message": "Internal server error", "type": "server_error"}}, 500)

    def _chat_completions(self, payload: dict[str, object]) -> None:
        if str(payload.get("model") or "") != "mro-request-assessment":
            return self._json({"error": {"message": "Unknown model", "type": "invalid_request_error"}}, 404)
        stream = _validate_stream_flag(payload)
        request_payload = _assessment_payload_from_chat(payload)
        if stream:
            return self._stream(request_payload)
        events: list[ProgressEvent] = []
        state = SERVICE.create_assessment(request_payload, events.append)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        return self._json({
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mro-request-assessment",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": build_final_content(state)}, "finish_reason": "stop"}],
            "assessment": state.model_dump(mode="json"),
            "reasoning_events": [item.model_dump(mode="json") for item in events],
        })

    def _stream(self, request_payload: dict[str, object]) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        events: queue.Queue[ProgressEvent | str] = queue.Queue()
        result_box: dict[str, AssessmentState | str] = {}

        def work() -> None:
            try:
                result_box["state"] = SERVICE.create_assessment(request_payload, events.put)
            except Exception:
                result_box["error"] = "failed"
            finally:
                events.put("_finished")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(delta: dict[str, object], finish_reason: str | None = None) -> None:
            chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": "mro-request-assessment", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        emit({"role": "assistant"})
        threading.Thread(target=work, daemon=True).start()
        while True:
            item = events.get()
            if item == "_finished":
                break
            if isinstance(item, ProgressEvent):
                emit({"reasoning_content": event_to_reasoning(item)})
        state = result_box.get("state")
        content = "Не удалось завершить preliminary assessment." if not isinstance(state, AssessmentState) else build_final_content(state)
        emit({"content": content})
        emit({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        return


def _assessment_payload_from_chat(payload: dict[str, object]) -> dict[str, object]:
    messages = payload.get("messages") or []
    text = ""
    if isinstance(messages, list):
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                text = str(item.get("content") or "").strip()
                if text:
                    break
    return {
        "request": text,
        "fields": payload.get("fields") if isinstance(payload.get("fields"), dict) else {},
        "business_context": payload.get("business_context") if isinstance(payload.get("business_context"), dict) else {},
    }


def _validate_stream_flag(payload: dict[str, object]) -> bool:
    if "stream" not in payload:
        return False
    if not isinstance(payload["stream"], bool):
        raise ValueError("stream must be a boolean")
    return payload["stream"]


def main() -> None:
    parser = argparse.ArgumentParser(description="MRO Request Assessment API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), RequestAssessmentHandler).serve_forever()


if __name__ == "__main__":
    main()
