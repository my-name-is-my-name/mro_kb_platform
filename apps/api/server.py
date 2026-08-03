from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import ensure_runtime_dirs
from core.ata_impact.http_contract import (
    extract_ata_request_text,
    merge_ata_request_fields,
    validate_stream_flag,
)
from core.ata_impact.modes import validate_ata_runtime_mode
from core.commercial_offers import CommercialOffersService
from core.go_no_go import AtaImpactAgent, GoNoGoService
from core.retrieval.service import RetrievalService
from ingest.mro_docs.import_documents import import_mro_documents
from ingest.publish_obsidian.publish import publish_obsidian_vault
from storage.sqlite.store import SQLiteStore


class RuntimeServices:
    def __init__(self) -> None:
        self.paths = ensure_runtime_dirs()
        self.store = SQLiteStore(self.paths.sqlite_path)
        self.store.initialize()
        self.retrieval = RetrievalService(self.store)
        self.commercial_offers = CommercialOffersService()
        self.go_no_go = GoNoGoService(self.store, self.commercial_offers)
        self.ata_impact = AtaImpactAgent(self.go_no_go.certificate, self.go_no_go.ata_catalog, self.go_no_go.retriever)

    def run_ingest(self) -> dict[str, object]:
        demo_root = self.paths.mro_rag_root / "apps" / "webapp" / "demo_data"
        cases, documents, chunks, doc_hash = import_mro_documents(demo_root)
        links = [(row["case_id"], row["document_id"], "matched") for row in documents]
        publish_obsidian_vault(self.paths.obsidian_vault_root, cases, chunks)
        self.store.replace_cases(cases)
        self.store.replace_documents_and_chunks(documents, chunks, links)
        doc_snapshot_id = self.store.write_snapshot(str(demo_root), doc_hash, "mro_rag")
        return {
            "ok": True,
            "doc_snapshot_id": doc_snapshot_id,
            "stats": self.store.stats(),
            "warnings": ["mro_kb_vector_index_stale_run_reindex_mro_kb_vectors"],
        }

    def reindex_mro_kb_vectors(self, limit: int = 0) -> dict[str, object]:
        return self.retrieval.reindex_vectors(limit=limit)

    def mro_kb_vectors_status(self) -> dict[str, object]:
        return self.retrieval.vector_status()


SERVICES = RuntimeServices()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MROKBPlatform/0.1"

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body_text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_openai_completion(self, model: str, result: dict[str, object], stream: bool) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        content = str(result.get("answer") or "")
        if not stream:
            return self._send_json(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "sources": result.get("sources") or [],
                    "warnings": result.get("warnings") or [],
                    "triage": result if model == "mro-go-no-go" else None,
                    "ata_impact": result if model == "mro-ata-impact" else None,
                }
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
            {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _send_ata_impact_stream(self, question: str, mode: str = "auto") -> None:
        """Expose safe tool progress in OpenWebUI's collapsible reasoning area."""
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        events: queue.Queue[dict[str, object]] = queue.Queue()
        result_box: dict[str, object] = {}

        def run_agent() -> None:
            try:
                result_box["result"] = SERVICES.ata_impact.analyze(
                    question,
                    progress=events.put,
                    mode=mode,
                )
            except Exception as exc:
                result_box["error"] = str(exc)
            finally:
                events.put({"stage": "_finished"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def send_delta(delta: dict[str, object], finish_reason: str | None = None) -> None:
            payload = {"id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": "mro-ata-impact", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        send_delta({"role": "assistant"})
        threading.Thread(target=run_agent, name="ata-impact-react", daemon=True).start()
        while True:
            event = events.get()
            if event.get("stage") == "_finished":
                break
            message = str(event.get("message") or "")
            if message:
                send_delta({"reasoning_content": message + "\n"})
        if result_box.get("error"):
            content = "Не удалось завершить предварительную оценку ATA. Проверьте доступность источников и повторите запрос."
        else:
            result = result_box.get("result") if isinstance(result_box.get("result"), dict) else {}
            content = str(result.get("answer") or "")
        send_delta({"content": content})
        send_delta({}, finish_reason="stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self._send_json({"status": "ok"})
            if parsed.path == "/v1/models":
                now = int(time.time())
                return self._send_json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "mro-kb",
                                "object": "model",
                                "created": now,
                                "owned_by": "local",
                            },
                            {
                                "id": "mro-similar-cases",
                                "object": "model",
                                "created": now,
                                "owned_by": "local",
                            },
                            {
                                "id": "mro-go-no-go",
                                "object": "model",
                                "created": now,
                                "owned_by": "local",
                            },
                        ],
                    }
                )
            if parsed.path == "/api/health":
                return self._send_json(
                    {
                        "ok": True,
                        "stats": SERVICES.store.stats(),
                        "components": SERVICES.retrieval.health(),
                        "commercial_offers": SERVICES.commercial_offers.health(),
                        "go_no_go": SERVICES.go_no_go.health(),
                    }
                )
            if parsed.path.startswith("/api/com-offers/registry/"):
                case_id = parsed.path.rsplit("/", 1)[-1]
                query = parse_qs(parsed.query)
                if query.get("format") == ["md"]:
                    markdown = SERVICES.commercial_offers.registry_case_markdown(case_id)
                    if markdown is None:
                        return self._send_json({"ok": False, "error": "commercial offer case not found"}, status=404)
                    return self._send_text(markdown, content_type="text/markdown; charset=utf-8")
                html = SERVICES.commercial_offers.registry_case_html(case_id)
                if html is None:
                    return self._send_json({"ok": False, "error": "commercial offer case not found"}, status=404)
                return self._send_text(html, content_type="text/html; charset=utf-8")
            if parsed.path == "/api/cases":
                return self._send_json({"ok": True, "cases": SERVICES.store.fetch_cases()})
            if parsed.path.startswith("/api/cases/"):
                case_id = parsed.path.rsplit("/", 1)[-1]
                payload = SERVICES.store.fetch_case(case_id)
                if payload is None:
                    return self._send_json({"ok": False, "error": "case not found"}, status=404)
                return self._send_json({"ok": True, "case": payload})
            if parsed.path.startswith("/api/documents/"):
                document_id = parsed.path.removeprefix("/api/documents/").replace("%2F", "/")
                payload = SERVICES.store.fetch_document(document_id)
                if payload is None:
                    return self._send_json({"ok": False, "error": "document not found"}, status=404)
                return self._send_json({"ok": True, "document": payload})
            if parsed.path.startswith("/api/chunks/"):
                chunk_id = parsed.path.removeprefix("/api/chunks/").replace("%2F", "/")
                payload = SERVICES.store.fetch_chunk(chunk_id)
                if payload is None:
                    return self._send_json({"ok": False, "error": "chunk not found"}, status=404)
                return self._send_json({"ok": True, "chunk": payload})
            return self._send_json({"ok": False, "error": "not found"}, status=404)
        except Exception as exc:
            return self._send_json(
                {"error": {"message": "Internal server error", "type": "server_error"}},
                status=500,
            )

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/ingest/run":
                return self._send_json(SERVICES.run_ingest())
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                return self._send_json({"error": {"message": "JSON body must be an object", "type": "invalid_request_error"}}, status=400)
            if parsed.path == "/v1/chat/completions":
                model = str(payload.get("model") or "").strip()
                if model not in {"mro-kb", "mro-similar-cases", "mro-go-no-go"}:
                    return self._send_json({"error": {"message": f"Unknown model: {model}", "type": "invalid_request_error"}}, status=404)
                messages = payload.get("messages") or []
                question = ""
                if isinstance(messages, list):
                    for item in reversed(messages):
                        if isinstance(item, dict) and str(item.get("role") or "") == "user":
                            question = str(item.get("content") or "").strip()
                            if question:
                                break
                if not question:
                    return self._send_json({"error": {"message": "User message is required", "type": "invalid_request_error"}}, status=400)
                if model == "mro-similar-cases":
                    result = SERVICES.commercial_offers.similar_cases(question)
                elif model == "mro-go-no-go":
                    result = SERVICES.go_no_go.triage(question)
                else:
                    result = SERVICES.retrieval.chat(question)
                return self._send_openai_completion(
                    model,
                    result,
                    validate_stream_flag(payload),
                )
            if parsed.path == "/api/triage":
                request_text = str(payload.get("request") or payload.get("q") or payload.get("question") or "").strip()
                if not request_text:
                    return self._send_json({"ok": False, "error": "request is required"}, status=400)
                fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
                return self._send_json({"ok": True, "triage": SERVICES.go_no_go.triage(request_text, fields)})
            if parsed.path == "/api/ata-impact":
                return self._send_json(
                    {
                        "ok": False,
                        "error": "mro-ata-impact is not served on port 8121; use http://10.100.112.51:8122",
                    },
                    status=410,
                )
            if parsed.path == "/api/chat":
                question = str(payload.get("q") or payload.get("question") or "").strip()
                if not question:
                    return self._send_json({"ok": False, "error": "q is required"}, status=400)
                return self._send_json({"ok": True, **SERVICES.retrieval.chat(question)})
            return self._send_json({"ok": False, "error": "not found"}, status=404)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_json({"error": {"message": "Invalid JSON", "type": "invalid_request_error"}}, status=400)
        except ValueError as exc:
            return self._send_json(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status=400,
            )
        except Exception:
            return self._send_json(
                {"error": {"message": "Internal server error", "type": "server_error"}},
                status=500,
            )

    def log_message(self, format: str, *args: object) -> None:
        return


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MRO KB Platform API")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=[
            "serve",
            "reindex-com-offers",
            "reindex-com-offers-status",
            "reindex-mro-kb-vectors",
            "reindex-mro-kb-vectors-status",
            "rebuild-com-offers-manifest",
            "publish-com-offer-registry",
            "build-com-offer-profiles",
            "com-offer-profiles-status",
            "reindex-com-offer-profile-vectors",
            "com-offer-profile-vectors-status",
        ],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    parser.add_argument("--limit", type=int, default=0, help="Limit background build commands; 0 means all cases")
    parser.add_argument("--force", action="store_true", help="Rebuild cached experimental profiles instead of reusing them")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.command == "rebuild-com-offers-manifest":
        result = SERVICES.commercial_offers.rebuild_converted_markdown_manifest()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "publish-com-offer-registry":
        result = SERVICES.commercial_offers.publish_registry_pages(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "reindex-com-offers":
        result = SERVICES.commercial_offers.reindex_case_vectors()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "reindex-com-offers-status":
        result = SERVICES.commercial_offers.reindex_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "reindex-mro-kb-vectors":
        result = SERVICES.reindex_mro_kb_vectors(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "reindex-mro-kb-vectors-status":
        result = SERVICES.mro_kb_vectors_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "build-com-offer-profiles":
        result = SERVICES.commercial_offers.build_case_profiles(limit=args.limit, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "com-offer-profiles-status":
        result = SERVICES.commercial_offers.case_profiles_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "reindex-com-offer-profile-vectors":
        result = SERVICES.commercial_offers.reindex_case_profile_vectors()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "com-offer-profile-vectors-status":
        result = SERVICES.commercial_offers.case_profile_vectors_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"MRO KB API started at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
