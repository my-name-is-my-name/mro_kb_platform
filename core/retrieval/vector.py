from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from core.config import DATA_RUNTIME_ROOT
from storage.sqlite.store import SQLiteStore


try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
except Exception:  # pragma: no cover - exercised when optional dependency is absent
    QdrantClient = None  # type: ignore[assignment]
    Distance = None  # type: ignore[assignment]
    PointStruct = None  # type: ignore[assignment]
    VectorParams = None  # type: ignore[assignment]


TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9.-]{3,}")
CLAUSE_REF_RE = re.compile(r"\b\d+(?:\.\d+){1,4}\b")
CYRILLIC_TOKEN_RE = re.compile(r"^[а-яё]+$")
LATIN_TOKEN_RE = re.compile(r"^[a-z]+$")
KEYWORD_FIELDS = {
    "text": 1.0,
    "search_text": 1.0,
    "section_title": 0.3,
    "section_label": 0.25,
    "heading_path": 0.2,
    "document_family": 0.15,
    "source_document_id": 0.1,
    "subject": 0.2,
    "problem_summary": 0.2,
}
MRO_QDRANT_SCHEMA_VERSION = 3


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_search_text(text: str) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = normalized.replace("–", "-").replace("—", "-").replace("−", "-")
    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
    normalized = re.sub(r"[_/\\]+", " ", normalized)
    normalized = re.sub(r"[()\\[\\]{}:;,!?\"'`]+", " ", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    return normalize_spaces(normalized)


def stem_token(token: str) -> str:
    if len(token) < 5:
        return token
    if CYRILLIC_TOKEN_RE.match(token):
        for suffix in (
            "иями",
            "ями",
            "ами",
            "его",
            "ого",
            "ему",
            "ому",
            "ыми",
            "ими",
            "ение",
            "ений",
            "ению",
            "ениях",
            "ость",
            "ости",
            "овать",
            "ирует",
            "ировать",
            "ается",
            "яются",
            "емый",
            "ения",
            "ением",
            "аний",
            "ания",
            "аться",
            "яться",
            "ющий",
            "ющая",
            "ющее",
            "ющие",
            "ющих",
            "ий",
            "ый",
            "ой",
            "ая",
            "яя",
            "ое",
            "ее",
            "ые",
            "ие",
            "ам",
            "ям",
            "ах",
            "ях",
            "ом",
            "ем",
            "ов",
            "ев",
            "ей",
            "ия",
            "ью",
            "а",
            "я",
            "ы",
            "и",
            "у",
            "ю",
            "е",
            "о",
        ):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                return token[: -len(suffix)]
    if LATIN_TOKEN_RE.match(token):
        for suffix in ("ing", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    return [stem_token(token) for token in TOKEN_RE.findall(normalize_search_text(text))]


def point_id_for_chunk(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mro-kb-chunk:{chunk_id}"))


def parent_id_for_chunk(chunk: dict[str, object]) -> str:
    section_title = normalize_spaces(str(chunk.get("section_title") or ""))
    document_id = str(chunk.get("document_id") or "")
    if section_title:
        return f"{document_id}::section::{hashlib.sha1(section_title.encode('utf-8')).hexdigest()[:16]}"
    return f"{document_id}::document"


@dataclass(frozen=True, slots=True)
class MroVectorSettings:
    enabled: bool = os.getenv("MRO_KB_QDRANT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    qdrant_url: str = os.getenv("MRO_KB_QDRANT_URL", "http://127.0.0.1:6333").strip().rstrip("/")
    collection: str = os.getenv("MRO_KB_QDRANT_COLLECTION", "mro_kb_chunks").strip()
    embedding_url: str = os.getenv("MRO_KB_OLLAMA_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    embedding_model: str = os.getenv("MRO_KB_EMBEDDING_MODEL", "bge-m3").strip()
    top_k: int = int(os.getenv("MRO_KB_TOP_K", "6"))
    retrieval_top_k: int = int(os.getenv("MRO_KB_RETRIEVAL_TOP_K", "50"))
    hybrid_enabled: bool = os.getenv("MRO_KB_HYBRID_SEARCH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    rrf_k: int = int(os.getenv("MRO_KB_RRF_K", "60"))
    batch_size: int = int(os.getenv("MRO_KB_QDRANT_UPSERT_BATCH_SIZE", "16"))
    embedding_batch_size: int = int(os.getenv("MRO_KB_EMBEDDING_BATCH_SIZE", "1"))
    target_total: int = int(os.getenv("MRO_KB_QDRANT_TARGET_TOTAL", "35566"))
    progress_path: Path = DATA_RUNTIME_ROOT / "mro_kb_qdrant_reindex_progress.json"


class OllamaEmbeddingClient:
    def __init__(self, base_url: str, model: str, timeout: int = 300) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        errors = []
        text = text[:3000]
        for path, payload in (
            ("/api/embeddings", {"model": self.model, "prompt": text}),
            ("/api/embed", {"model": self.model, "input": text}),
        ):
            request = urllib.request.Request(
                f"{self.base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                errors.append(repr(exc))
                continue
            if isinstance(body.get("embedding"), list):
                return [float(value) for value in body["embedding"]]
            embeddings = body.get("embeddings")
            if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
                return [float(value) for value in embeddings[0]]
        raise RuntimeError(f"Ollama embeddings unavailable: {'; '.join(errors)}")

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) == 1:
            return [self.embed(texts[0])]
        prepared = [text[:3000] for text in texts]
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=json.dumps({"model": self.model, "input": prepared, "keep_alive": "30m"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            embeddings = body.get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                return [[float(value) for value in vector] for vector in embeddings]
        except Exception:
            pass
        return [self.embed(text) for text in texts]


class RestQdrantClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_collections(self) -> object:
        body = self._request("GET", "/collections")
        collections = ((body.get("result") or {}).get("collections") or []) if isinstance(body.get("result"), dict) else []
        return SimpleNamespace(collections=[SimpleNamespace(name=str(item.get("name") or "")) for item in collections if isinstance(item, dict)])

    def create_collection(self, collection_name: str, vectors_config: object) -> None:
        size = int(vectors_config) if isinstance(vectors_config, int) else int(getattr(vectors_config, "size"))
        self._request(
            "PUT",
            f"/collections/{collection_name}",
            {"vectors": {"size": size, "distance": "Cosine"}},
        )

    def delete_collection(self, collection_name: str) -> None:
        self._request("DELETE", f"/collections/{collection_name}")

    def count(self, collection_name: str, exact: bool = False) -> object:
        body = self._request("POST", f"/collections/{collection_name}/points/count", {"exact": exact})
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        return SimpleNamespace(count=int(result.get("count") or 0))

    def upsert(self, collection_name: str, points: list[object], wait: bool = True) -> None:
        serializable = []
        for point in points:
            if isinstance(point, dict):
                serializable.append(point)
                continue
            serializable.append(
                {
                    "id": str(getattr(point, "id")),
                    "vector": list(getattr(point, "vector")),
                    "payload": dict(getattr(point, "payload")),
                }
            )
        self._request("PUT", f"/collections/{collection_name}/points?wait={str(wait).lower()}", {"points": serializable})

    def scroll(
        self,
        collection_name: str,
        limit: int,
        offset: object | None = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> tuple[list[object], object | None]:
        payload: dict[str, object] = {
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vectors,
        }
        if offset is not None:
            payload["offset"] = offset
        body = self._request("POST", f"/collections/{collection_name}/points/scroll", payload)
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        points = result.get("points") if isinstance(result.get("points"), list) else []
        return [SimpleNamespace(payload=dict(item.get("payload") or {})) for item in points if isinstance(item, dict)], result.get("next_page_offset")

    def search(self, collection_name: str, query_vector: list[float], limit: int, with_payload: bool = True) -> list[object]:
        body = self._request(
            "POST",
            f"/collections/{collection_name}/points/search",
            {"vector": query_vector, "limit": limit, "with_payload": with_payload},
        )
        result = body.get("result") if isinstance(body.get("result"), list) else []
        return [
            SimpleNamespace(score=float(item.get("score") or 0.0), payload=dict(item.get("payload") or {}))
            for item in result
            if isinstance(item, dict)
        ]


class MroQdrantIndex:
    def __init__(self, store: SQLiteStore, settings: MroVectorSettings | None = None) -> None:
        self.store = store
        self.settings = settings or MroVectorSettings()
        self.embedding_client = OllamaEmbeddingClient(self.settings.embedding_url, self.settings.embedding_model)

    def health(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.settings.enabled,
            "ready": False,
            "collection": self.settings.collection,
            "qdrant_url": self.settings.qdrant_url,
            "model": self.settings.embedding_model,
            "schema_version": MRO_QDRANT_SCHEMA_VERSION,
            "warnings": [],
        }
        if not self.settings.enabled:
            payload["warnings"] = ["mro_kb_qdrant_disabled"]
            return payload
        try:
            qdrant = self._client()
            count = self._point_count(qdrant)
            payload["points"] = count
            payload["ready"] = count > 0
            progress = self.reindex_status()
            payload["progress"] = progress.get("status")
            if progress.get("schema_version") not in {None, MRO_QDRANT_SCHEMA_VERSION}:
                payload["stale"] = True
                payload["ready"] = False
                payload["warnings"] = ["mro_kb_vector_schema_changed_run_reindex_mro_kb_vectors"]
            elif count <= 0:
                payload["warnings"] = ["mro_kb_vector_index_empty_run_reindex_mro_kb_vectors"]
        except Exception as exc:
            payload["warnings"] = [f"mro_kb_qdrant_unavailable: {exc!r}"]
        return payload

    def reindex(self, limit: int = 0) -> dict[str, object]:
        started = time.time()
        qdrant = self._client()
        existing_parent_ids = set() if limit > 0 else self._existing_parent_ids(qdrant)
        total = limit if limit > 0 else self.settings.target_total
        corpus_hash = f"sqlite_parent_sections:{total}:schema:{MRO_QDRANT_SCHEMA_VERSION}:model:{self.settings.embedding_model}"
        done = len(existing_parent_ids)
        failures: list[str] = []
        self._write_progress(
            {
                "status": "running",
                "started_at": self._timestamp(started),
                "updated_at": self._timestamp(),
                "total": total,
                "done": done,
                "skipped_existing": len(existing_parent_ids),
                "failures": [],
                "failure_count": 0,
                "collection": self.settings.collection,
                "qdrant_url": self.settings.qdrant_url,
                "model": self.settings.embedding_model,
                "schema_version": MRO_QDRANT_SCHEMA_VERSION,
                "corpus_hash": corpus_hash,
            }
        )
        vector_size = 0
        collection_ready = bool(existing_parent_ids)
        pending_chunks: list[dict[str, object]] = []

        def flush_pending() -> None:
            nonlocal done, vector_size, collection_ready, pending_chunks
            if not pending_chunks:
                return
            texts = [self.embedding_text(chunk) for chunk in pending_chunks]
            vectors = self.embedding_client.embed_many(texts)
            points: list[Any] = []
            for chunk, vector in zip(pending_chunks, vectors):
                if not vector:
                    failures.append(str(chunk.get("chunk_id") or ""))
                    continue
                if not vector_size:
                    vector_size = len(vector)
                if not collection_ready:
                    if limit <= 0:
                        self._reset_collection(qdrant, vector_size)
                    else:
                        self._ensure_collection(qdrant, vector_size)
                    collection_ready = True
                parent_id = str(chunk.get("parent_id") or parent_id_for_chunk(chunk))
                points.append(self._make_point(point_id_for_chunk(parent_id), vector, self.payload_for_chunk(chunk)))
            if points:
                qdrant.upsert(collection_name=self.settings.collection, points=points, wait=True)
                done += len(points)
            pending_chunks = []

        for chunk in self.store.iter_parent_sections(batch_size=512, limit=limit, skip_parent_ids=existing_parent_ids):
            try:
                parent_id = str(chunk.get("parent_id") or parent_id_for_chunk(chunk))
                if parent_id in existing_parent_ids:
                    continue
                pending_chunks.append(chunk)
                if len(pending_chunks) >= self.settings.embedding_batch_size:
                    flush_pending()
            except Exception:
                failures.append(str(chunk.get("chunk_id") or ""))
            if (done + len(pending_chunks)) % max(self.settings.batch_size * 10, 1) == 0:
                self._write_progress(
                    {
                        "status": "running",
                        "started_at": self._timestamp(started),
                        "updated_at": self._timestamp(),
                        "total": total,
                        "done": done + len(pending_chunks),
                        "skipped_existing": len(existing_parent_ids),
                        "failure_count": len(failures),
                        "failures": failures[-20:],
                        "collection": self.settings.collection,
                        "qdrant_url": self.settings.qdrant_url,
                        "model": self.settings.embedding_model,
                        "schema_version": MRO_QDRANT_SCHEMA_VERSION,
                        "corpus_hash": corpus_hash,
                    }
                )
        flush_pending()
        final_total = total or done + len(failures)
        status = "complete" if done and not failures and done == final_total else "partial"
        result = {
            "ok": bool(done),
            "status": status,
            "total": final_total,
            "done": done,
            "skipped_existing": len(existing_parent_ids),
            "failure_count": len(failures),
            "failures": failures[-50:],
            "collection": self.settings.collection,
            "qdrant_url": self.settings.qdrant_url,
            "model": self.settings.embedding_model,
            "schema_version": MRO_QDRANT_SCHEMA_VERSION,
            "corpus_hash": corpus_hash,
            "seconds": round(time.time() - started, 2),
        }
        self._write_progress({**result, "updated_at": self._timestamp(), "started_at": self._timestamp(started)})
        return result

    def reindex_status(self) -> dict[str, object]:
        try:
            payload = json.loads(self.settings.progress_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"status": "missing", "progress": str(self.settings.progress_path)}
        except Exception as exc:
            payload = {"status": "unreadable", "error": repr(exc), "progress": str(self.settings.progress_path)}
        payload["collection"] = self.settings.collection
        payload["qdrant_url"] = self.settings.qdrant_url
        payload["model"] = self.settings.embedding_model
        done = int(payload.get("done") or 0)
        target_total = int(payload.get("target_total") or payload.get("total") or self.settings.target_total or 0)
        if target_total > 0:
            payload["target_total"] = target_total
            payload["total"] = target_total
            payload["remaining"] = max(target_total - done, 0)
            payload["percent_complete"] = round(min(done / target_total, 1.0) * 100, 2)
        return payload

    def search(self, question: str, limit: int, explicit_case_id: str | None = None) -> tuple[list[dict[str, object]], list[str]]:
        if not self.settings.enabled:
            return [], ["mro_kb_qdrant_disabled"]
        if QdrantClient is None:
            qdrant_mode = "qdrant_rest"
        else:
            qdrant_mode = "qdrant_client"
        try:
            query_vector = self.embedding_client.embed(question)
            qdrant = self._client()
            search_limit = max(limit, self.settings.retrieval_top_k)
            hits = self._vector_search(qdrant, query_vector, search_limit)
        except Exception as exc:
            return [], [f"mro_kb_vector_search_fallback: {exc!r}"]
        rows: list[dict[str, object]] = []
        for rank, hit in enumerate(hits, start=1):
            payload = dict(getattr(hit, "payload", None) or {})
            if explicit_case_id and payload.get("case_id") != explicit_case_id:
                continue
            payload["vector_rank"] = rank
            payload["vector_score"] = float(getattr(hit, "score", 0.0))
            payload["rrf_score"] = 1 / (self.settings.rrf_k + rank)
            payload["retrieval_mode"] = qdrant_mode
            rows.append(payload)
        return rows, []

    def hybrid_merge(
        self,
        question: str,
        vector_hits: list[dict[str, object]],
        lexical_hits: list[dict[str, object]],
        limit: int,
    ) -> list[dict[str, object]]:
        merged: dict[str, dict[str, object]] = {}
        for rank, hit in enumerate(vector_hits, start=1):
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id:
                continue
            updated = dict(hit)
            updated["rrf_score"] = float(updated.get("rrf_score", 0.0)) or 1 / (self.settings.rrf_k + rank)
            merged[chunk_id] = updated
        for rank, hit in enumerate(lexical_hits, start=1):
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id:
                continue
            lexical_rrf = 1 / (self.settings.rrf_k + rank)
            if chunk_id in merged:
                merged[chunk_id].update({key: value for key, value in hit.items() if key not in merged[chunk_id] or not merged[chunk_id].get(key)})
                merged[chunk_id]["lexical_rank"] = rank
                merged[chunk_id]["lexical_score"] = float(hit.get("lexical_score", 0.0))
                merged[chunk_id]["rrf_score"] = float(merged[chunk_id].get("rrf_score", 0.0)) + lexical_rrf
            else:
                updated = dict(hit)
                updated["lexical_rank"] = rank
                updated["rrf_score"] = lexical_rrf
                merged[chunk_id] = updated
        rescored = []
        for hit in merged.values():
            updated = dict(hit)
            updated["hybrid_score"] = float(updated.get("rrf_score", 0.0)) + self._structure_bonus(question, updated)
            updated["retrieval_mode"] = "qdrant_hybrid" if updated.get("vector_score") is not None else "sqlite_like"
            rescored.append(updated)
        rescored.sort(key=lambda item: float(item.get("hybrid_score", 0.0)), reverse=True)
        return self._diversify(rescored, limit)

    @staticmethod
    def embedding_text(chunk: dict[str, object]) -> str:
        heading_path = chunk.get("heading_path")
        if isinstance(heading_path, list):
            heading = " > ".join(str(item) for item in heading_path)
        else:
            heading = str(heading_path or chunk.get("heading_path_json") or "")
        header = "\n".join(
            part
            for part in [
                f"Заявка: {chunk.get('case_id', '')}",
                f"Тип ВС: {chunk.get('aircraft_type', '')}",
                f"MSN: {chunk.get('msn', '')}",
                f"ATA: {chunk.get('ata_list', '') or chunk.get('document_ata', '')}",
                f"Тема заявки: {chunk.get('subject', '')}",
                f"Проблема: {chunk.get('problem_summary', '')}",
                f"Документ: {chunk.get('document_family', '')} {chunk.get('title', '') or chunk.get('document_title', '')}",
                f"ID документа: {chunk.get('source_document_id', '')}",
                f"Раздел: {chunk.get('section_title', '')}",
                f"Метка раздела: {chunk.get('section_label', '')}",
                f"Parent section: {parent_id_for_chunk(chunk)}",
                f"Child chunks: {chunk.get('child_count', '')}",
                f"Путь заголовков: {heading}",
                f"Тип чанка: {chunk.get('chunk_kind', '')} {chunk.get('chunk_level', '')}",
            ]
            if normalize_spaces(part)
        )
        body = str(chunk.get("search_text") or chunk.get("text") or "")
        return f"{header}\n\n{body[:2200]}"[:3000]

    @staticmethod
    def payload_for_chunk(chunk: dict[str, object]) -> dict[str, object]:
        parent_id = parent_id_for_chunk(chunk)
        payload = {
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "parent_id": parent_id,
            "parent_kind": "section" if normalize_spaces(str(chunk.get("section_title") or "")) else "document",
            "child_chunk_ids": chunk.get("child_chunk_ids") if isinstance(chunk.get("child_chunk_ids"), list) else [str(chunk.get("chunk_id") or "")],
            "child_count": int(chunk.get("child_count") or 1),
            "case_id": str(chunk.get("case_id") or ""),
            "document_id": str(chunk.get("document_id") or ""),
            "source_document_id": str(chunk.get("source_document_id") or ""),
            "document_family": str(chunk.get("document_family") or ""),
            "chunk_kind": str(chunk.get("chunk_kind") or ""),
            "unit_kind": str(chunk.get("unit_kind") or ""),
            "chunk_level": str(chunk.get("chunk_level") or ""),
            "section_title": str(chunk.get("section_title") or ""),
            "section_label": str(chunk.get("section_label") or ""),
            "heading_path": chunk.get("heading_path") if isinstance(chunk.get("heading_path"), list) else [],
            "subject": str(chunk.get("subject") or ""),
            "problem_summary": str(chunk.get("problem_summary") or ""),
            "aircraft_type": str(chunk.get("aircraft_type") or ""),
            "msn": str(chunk.get("msn") or ""),
            "ata_list": chunk.get("ata_list") if isinstance(chunk.get("ata_list"), list) else [],
            "source_file": str(chunk.get("source_file") or ""),
            "vault_note_path": str(chunk.get("vault_note_path") or ""),
            "block_id": str(chunk.get("block_id") or ""),
            "page_image_path": str(chunk.get("page_image_path") or ""),
            "text": str(chunk.get("text") or "")[:5000],
            "search_text": str(chunk.get("search_text") or "")[:5000],
        }
        return payload

    def _client(self) -> Any:
        if QdrantClient is not None:
            return QdrantClient(url=self.settings.qdrant_url)
        return RestQdrantClient(self.settings.qdrant_url)

    @staticmethod
    def _make_point(point_id: str, vector: list[float], payload: dict[str, object]) -> object:
        if PointStruct is not None:
            return PointStruct(id=point_id, vector=vector, payload=payload)
        return {"id": point_id, "vector": vector, "payload": payload}

    def _ensure_collection(self, qdrant: Any, vector_size: int) -> None:
        collections = {collection.name for collection in qdrant.get_collections().collections}
        if self.settings.collection in collections:
            return
        qdrant.create_collection(
            collection_name=self.settings.collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE) if VectorParams is not None and Distance is not None else vector_size,
        )

    def _reset_collection(self, qdrant: Any, vector_size: int) -> None:
        collections = {collection.name for collection in qdrant.get_collections().collections}
        if self.settings.collection in collections:
            qdrant.delete_collection(collection_name=self.settings.collection)
        qdrant.create_collection(
            collection_name=self.settings.collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE) if VectorParams is not None and Distance is not None else vector_size,
        )

    def _point_count(self, qdrant: Any) -> int:
        try:
            return int(qdrant.count(collection_name=self.settings.collection, exact=False).count)
        except Exception:
            return 0

    def _existing_parent_ids(self, qdrant: Any) -> set[str]:
        collections = {collection.name for collection in qdrant.get_collections().collections}
        if self.settings.collection not in collections:
            return set()
        parent_ids: set[str] = set()
        offset: object | None = None
        while True:
            points, offset = self._scroll_points(qdrant, offset=offset, limit=1024)
            for point in points:
                payload = dict(getattr(point, "payload", None) or {})
                parent_id = str(payload.get("parent_id") or "")
                if parent_id:
                    parent_ids.add(parent_id)
            if offset is None:
                return parent_ids

    def _scroll_points(self, qdrant: Any, offset: object | None, limit: int) -> tuple[list[Any], object | None]:
        try:
            points, next_offset = qdrant.scroll(
                collection_name=self.settings.collection,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            return list(points), next_offset
        except TypeError:
            points, next_offset = qdrant.scroll(
                collection_name=self.settings.collection,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vector=False,
            )
            return list(points), next_offset

    def _vector_search(self, qdrant: Any, query_vector: list[float], limit: int) -> list[Any]:
        try:
            return list(
                qdrant.search(
                    collection_name=self.settings.collection,
                    query_vector=query_vector,
                    limit=limit,
                    with_payload=True,
                )
            )
        except AttributeError:
            result = qdrant.query_points(
                collection_name=self.settings.collection,
                query=query_vector,
                limit=limit,
                with_payload=True,
            )
            return list(result.points)

    def _structure_bonus(self, question: str, hit: dict[str, object]) -> float:
        question_tokens = set(tokenize(question))
        if not question_tokens:
            return 0.0
        bonus = 0.0
        for field, weight in (
            ("section_title", 0.12),
            ("section_label", 0.08),
            ("document_family", 0.06),
            ("source_document_id", 0.08),
            ("subject", 0.08),
            ("problem_summary", 0.08),
        ):
            value_tokens = set(tokenize(str(hit.get(field) or "")))
            if value_tokens:
                bonus += weight * (len(question_tokens & value_tokens) / len(value_tokens))
        clause_refs = set(CLAUSE_REF_RE.findall(question))
        section = str(hit.get("section_title") or "")
        if clause_refs and any(ref in section for ref in clause_refs):
            bonus += 0.2
        return bonus

    @staticmethod
    def _diversify(hits: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        selected = []
        seen_chunks = set()
        seen_parents = set()
        section_counts: Counter[tuple[str, str]] = Counter()
        for hit in hits:
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen_chunks:
                continue
            parent_id = str(hit.get("parent_id") or "")
            if parent_id and parent_id in seen_parents:
                continue
            key = (str(hit.get("document_id") or ""), str(hit.get("section_title") or ""))
            if section_counts[key] >= 3:
                continue
            seen_chunks.add(chunk_id)
            if parent_id:
                seen_parents.add(parent_id)
            section_counts[key] += 1
            selected.append(hit)
            if len(selected) >= limit:
                return selected
        for hit in hits:
            chunk_id = str(hit.get("chunk_id") or "")
            if chunk_id and chunk_id not in seen_chunks:
                selected.append(hit)
                seen_chunks.add(chunk_id)
                if len(selected) >= limit:
                    break
        return selected

    def _write_progress(self, payload: dict[str, object]) -> None:
        self.settings.progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.settings.progress_path.with_suffix(self.settings.progress_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.settings.progress_path)

    @staticmethod
    def _timestamp(value: float | None = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value or time.time()))


def corpus_hash_for_chunks(chunks: Iterable[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(str(chunk.get("chunk_id") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(chunk.get("search_text") or chunk.get("text") or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
