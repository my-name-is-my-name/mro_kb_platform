from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import ensure_runtime_dirs
from core.commercial_offers import CommercialOffersService
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
        }


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
                            }
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
                    }
                )
            if parsed.path.startswith("/api/com-offers/registry/"):
                case_id = parsed.path.rsplit("/", 1)[-1]
                markdown = SERVICES.commercial_offers.registry_case_markdown(case_id)
                if markdown is None:
                    return self._send_json({"ok": False, "error": "commercial offer case not found"}, status=404)
                return self._send_text(markdown, content_type="text/markdown; charset=utf-8")
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
            return self._send_json({"ok": False, "error": f"internal error: {exc}"}, status=500)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/ingest/run":
                return self._send_json(SERVICES.run_ingest())
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if parsed.path == "/v1/chat/completions":
                model = str(payload.get("model") or "").strip()
                if model not in {"mro-kb", "mro-similar-cases"}:
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
                else:
                    result = SERVICES.retrieval.chat(question)
                return self._send_json(
                    {
                        "id": f"chatcmpl-{uuid.uuid4().hex}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": str(result.get("answer") or ""),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "sources": result.get("sources") or [],
                        "warnings": result.get("warnings") or [],
                    }
                )
            if parsed.path == "/api/chat":
                question = str(payload.get("q") or payload.get("question") or "").strip()
                if not question:
                    return self._send_json({"ok": False, "error": "q is required"}, status=400)
                return self._send_json({"ok": True, **SERVICES.retrieval.chat(question)})
            return self._send_json({"ok": False, "error": "not found"}, status=404)
        except Exception as exc:
            return self._send_json({"ok": False, "error": f"internal error: {exc}"}, status=500)

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
