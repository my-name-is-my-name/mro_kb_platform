from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from core.config import DATA_RUNTIME_ROOT, WORKSPACE_ROOT
from core.runtime_clients import ExternalReranker, OpenAICompatibleLLM, RuntimeSettings


TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9.-]{2,}")
CASE_RE = re.compile(r"\b(?:MP|MRO|MR|МР)[-_\s.]*(\d{1,5})(?:[.,-](\d{1,3}))?\b", re.IGNORECASE)
AIRCRAFT_RE = re.compile(
    r"\b(?:(?:AIRBUS|BOEING)\s*)?(?:A|А|B|В|CRJ|CL|BD|RRJ|SSJ|EMB|ATR)\s*-?\s*\d{2,4}(?:[-\s]?(?:CL|NG|MAX|\d{2,4}))?\b|"
    r"\b(?:BOEING|AIRBUS)\s*\d{2,4}(?:[-\s]?(?:CL|NG|MAX|\d{2,4}))?\b|"
    r"\b\d{3}(?:CL|NG|MAX)\b",
    re.IGNORECASE,
)
ATA_RE = re.compile(r"\bATA\s*[-:]?\s*\d{2}(?:-\d{2})?\b", re.IGNORECASE)
RIB_RE = re.compile(r"\bRIB\s*[-#:]?\s*\d{1,3}(?:\s*[-–]\s*\d{1,3})?\b", re.IGNORECASE)
FRAME_RE = re.compile(r"\b(?:FR|FRAME|ШП|ШПАНГОУТ[А-Яа-я]*)\s*[.#№-]?\s*\d{1,3}(?:[.,]\d{1,2})?(?:\s*[-–]\s*\d{1,3})?\b", re.IGNORECASE)
STGR_RE = re.compile(r"\b(?:STGR|STRINGER|СТРИНГЕР)\s*[.#№-]?\s*\d{1,3}[A-ZА-Я]?\b", re.IGNORECASE)
PART_RE = re.compile(r"\b(?:P/N|PN|PART\s*NUMBER|П/Н)\s*[:#-]?\s*[A-Z0-9][A-Z0-9_.-]{3,}\b", re.IGNORECASE)
MSN_RE = re.compile(r"\bMSN\s*[:#-]?\s*\d{2,6}\b", re.IGNORECASE)
REG_RE = re.compile(r"\b(?:(?:RA|VP|VQ|EI|TC|HL)-?\d{3,6}[A-ZА-Я]?|N\d{3,6}[A-ZА-Я]?|B-\d{3,6}[A-ZА-Я]?)\b", re.IGNORECASE)
AD_RE = re.compile(r"\b(?:EASA\s*)?(?:FAA\s*)?(?:AD|ДЛГ|ДИРЕКТИВ[А-Яа-я]*\s+ЛГ)\s*(?:NO\.?\s*)?[:#-]?\s*\d{4}[-–]\d{2,4}(?:\s*R\.?\s*\d+)?\b", re.IGNORECASE)
SB_RE = re.compile(r"\b(?:SB|СБ|VSB)\s*[:#-]?\s*[A-Z]?\d{2,4}[-–]\d{2,5}(?:\s*R\.?\s*\d+)?\b", re.IGNORECASE)

STOP_TOKENS = {
    "заявка",
    "заявке",
    "заявку",
    "заявки",
    "похож",
    "похожие",
    "найди",
    "подбери",
    "для",
    "при",
    "как",
    "что",
    "где",
    "есть",
    "and",
    "анализ",
    "возможность",
    "выполнения",
    "выполнение",
    "временная",
    "временное",
    "изменение",
    "ограничения",
    "of",
    "for",
    "maintenance",
    "works",
    "проведения",
    "работ",
    "работы",
    "ремонтных",
    "разрешение",
    "the",
    "технический",
    "технического",
    "to",
    "with",
    "from",
    "request",
}
EXACT_PATTERNS = (ATA_RE, RIB_RE, FRAME_RE, STGR_RE, PART_RE, MSN_RE, REG_RE, AD_RE, SB_RE, CASE_RE)
CONVERTED_MARKDOWN_SCHEMA_VERSION = 1
CONVERTED_MARKDOWN_EXCLUDED_PARTS = {
    "archive",
    "archives",
    "example",
    "examples",
    "reference",
    "references",
    "tmp",
    "примеры",
    "пример",
    "архив",
    "справочно",
    "прочность",
}
CONVERTED_MARKDOWN_SKIP_RE = re.compile(r"(?:пример|example|archive|архив|\btmp\b)", re.IGNORECASE)
RANGE_DIR_RE = re.compile(r"^(?:MP|MRO|MR|МР)\s*\d{1,4}\s*[-–]\s*\d{1,4}$", re.IGNORECASE)
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SPREADSHEET_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEET_NAMESPACES = {"a": SPREADSHEET_NS, "r": SPREADSHEET_REL_NS}


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_lookup(text: str) -> str:
    return normalize_spaces((text or "").lower().replace("ё", "е"))


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in TOKEN_RE.findall(normalize_lookup(text)):
        if token in STOP_TOKENS:
            continue
        if len(token) < 3 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens


def normalize_case_id(value: str) -> str:
    match = CASE_RE.search(value or "")
    if not match:
        return value.strip()
    suffix = match.group(2)
    return f"MP-{int(match.group(1)):04d}" + (f".{suffix}" if suffix else "")


def exact_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for pattern in EXACT_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = normalize_spaces(match.group(0).upper().replace(" ", ""))
            if pattern is CASE_RE:
                value = normalize_case_id(match.group(0))
            if value and value not in seen:
                seen.add(value)
                terms.append(value)
    return terms


def aircraft_tokens(text: str) -> set[str]:
    return {normalize_lookup(match.group(0)).replace(" ", "").replace("-", "") for match in AIRCRAFT_RE.finditer(text or "")}


def strip_aircraft_terms(text: str) -> str:
    return normalize_spaces(AIRCRAFT_RE.sub(" ", text or ""))


class CommercialOffersService:
    def __init__(
        self,
        root: Path = WORKSPACE_ROOT / "com_offers",
        vector_cache_path: Path = DATA_RUNTIME_ROOT / "com_offers_case_vectors.json",
        converted_markdown_manifest_path: Path = DATA_RUNTIME_ROOT / "com_offers_converted_markdown_manifest.json",
        reindex_progress_path: Path = DATA_RUNTIME_ROOT / "com_offers_reindex_progress.json",
    ) -> None:
        self.root = root
        self.artifacts = root / "pilot_artifacts"
        self.registry_path = self.artifacts / "case_registry.csv"
        self.links_path = self.artifacts / "case_document_links.csv"
        self.documents_path = self.artifacts / "case_documents.jsonl"
        self.reestr_path = root / "Reestr_zayavok.xlsm"
        self.vector_cache_path = vector_cache_path
        self.converted_markdown_manifest_path = converted_markdown_manifest_path
        self.reindex_progress_path = reindex_progress_path
        self.embedding_url = os.getenv("MRO_KB_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.embedding_model = os.getenv("MRO_KB_EMBEDDING_MODEL", "bge-m3:latest")
        self.query_rewrite_enabled = os.getenv("MRO_KB_QUERY_REWRITE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        self.settings = RuntimeSettings()
        self._reranker = ExternalReranker(self.settings.reranker_url, batch_size=self.settings.reranker_batch_size) if (
            self.settings.reranker_enabled and self.settings.reranker_url
        ) else None
        self._llm = OpenAICompatibleLLM(self.settings) if self.settings.llm_enabled and self.settings.llm_provider == "openai" else None
        self._registry = self._load_registry()
        self._registry_by_case = {row.get("case_id", ""): row for row in self._registry}
        self._links = self._load_links()
        self._documents = self._load_documents()
        self._reestr_enrichment, self._reestr_status = self._load_reestr_enrichment()
        self._extra_search_texts, self._converted_markdown_status = self._read_converted_markdown_manifest()
        self._vectors, self._index_status = self._read_vector_cache()
        self._doc_frequency = self._build_doc_frequency()

    def health(self) -> dict[str, object]:
        warnings = list(self._index_status.get("warnings") or [])
        warnings.extend(self._converted_markdown_status.get("warnings") or [])
        warnings.extend(self._reestr_status.get("warnings") or [])
        return {
            "cases": len(self._registry),
            "linked_cases": len(self._links),
            "documents": len(self._documents),
            "converted_markdown_search_cases": len(self._extra_search_texts),
            "converted_markdown": {
                "ready": bool(self._extra_search_texts) and not bool(self._converted_markdown_status.get("stale")),
                "cases": len(self._extra_search_texts),
                "documents": self._converted_markdown_status.get("documents", 0),
                "skipped": self._converted_markdown_status.get("skipped", 0),
                "conflicts": self._converted_markdown_status.get("conflicts", 0),
                "cache": str(self.converted_markdown_manifest_path),
                "created_at": self._converted_markdown_status.get("created_at", ""),
            },
            "document_links": self._link_status_counts(),
            "reestr_zayavok": {
                "ready": bool(self._reestr_enrichment) and not bool(self._reestr_status.get("stale")),
                "cases": len(self._reestr_enrichment),
                "path": str(self.reestr_path),
                "warnings": self._reestr_status.get("warnings", []),
            },
            "case_embeddings": {
                "ready": bool(self._vectors) and not bool(self._index_status.get("stale")),
                "count": len(self._vectors),
                "expected_cases": len(self._registry),
                "partial": bool(self._index_status.get("partial")),
                "missing_count": self._index_status.get("missing_count", 0),
                "missing_cases": self._index_status.get("missing_cases", []),
                "stale": bool(self._index_status.get("stale")),
                "cache": str(self.vector_cache_path),
                "model": self.embedding_model,
                "base_url": self.embedding_url,
                "created_at": self._index_status.get("created_at", ""),
            },
            "warnings": warnings,
            "reranker": self._reranker.health() if self._reranker is not None else {"ok": False, "disabled": True},
            "llm": self._llm.health() if self._llm is not None else {"ok": False, "disabled": True},
        }

    def reindex_case_vectors(self) -> dict[str, object]:
        manifest = self.rebuild_converted_markdown_manifest()
        expected_hashes = {row.get("case_id", ""): self._text_hash(self._case_similarity_text(row)) for row in self._registry}
        vectors = self._load_reusable_vectors(expected_hashes)
        failures: list[str] = []
        started = time.time()
        reused_count = len(vectors)
        total = len(self._registry)
        self._write_reindex_progress(
            {
                "status": "running",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total": total,
                "done": reused_count,
                "reused": reused_count,
                "built": 0,
                "failures": [],
                "current_case_id": "",
                "cache": str(self.vector_cache_path),
                "model": self.embedding_model,
            }
        )
        built_count = 0
        for index, row in enumerate(self._registry, start=1):
            case_id = row.get("case_id", "")
            text = self._case_similarity_text(row)
            text_hash = expected_hashes.get(case_id, "")
            if case_id and case_id in vectors and vectors[case_id].get("text_hash") == text_hash:
                continue
            vector = self._embed_text(text)
            if case_id and vector:
                vectors[case_id] = {
                    "text_hash": text_hash,
                    "terms": exact_terms(text),
                    "vector": vector,
                }
                built_count += 1
            elif case_id:
                failures.append(case_id)
            if index % 10 == 0 or index == total or case_id in failures:
                self._write_reindex_progress(
                    {
                        "status": "running",
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "total": total,
                        "done": len(vectors),
                        "reused": reused_count,
                        "built": built_count,
                        "failure_count": len(failures),
                        "failures": failures[-20:],
                        "current_case_id": case_id,
                        "cache": str(self.vector_cache_path),
                        "model": self.embedding_model,
                    }
                )
                self._write_vector_cache(vectors)
        if not vectors:
            raise RuntimeError("No commercial offer vectors were built; check embedding endpoint")
        self._write_vector_cache(vectors)
        self._vectors, self._index_status = self._read_vector_cache()
        status = "complete" if len(vectors) == total and not failures else "partial"
        self._write_reindex_progress(
            {
                "status": status,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total": total,
                "done": len(vectors),
                "reused": reused_count,
                "built": built_count,
                "failure_count": len(failures),
                "failures": failures[-50:],
                "current_case_id": "",
                "cache": str(self.vector_cache_path),
                "model": self.embedding_model,
                "seconds": round(time.time() - started, 2),
            }
        )
        return {
            "ok": True,
            "cases": len(self._registry),
            "vectors": len(vectors),
            "status": status,
            "reused": reused_count,
            "built": built_count,
            "converted_markdown": manifest,
            "failures": failures[:20],
            "failure_count": len(failures),
            "seconds": round(time.time() - started, 2),
            "cache": str(self.vector_cache_path),
            "model": self.embedding_model,
        }

    def reindex_status(self) -> dict[str, object]:
        try:
            progress = json.loads(self.reindex_progress_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            progress = {"status": "missing", "progress": str(self.reindex_progress_path)}
        except (OSError, ValueError) as exc:
            progress = {"status": "unreadable", "error": repr(exc), "progress": str(self.reindex_progress_path)}
        try:
            payload = json.loads(self.vector_cache_path.read_text(encoding="utf-8"))
            vectors = payload.get("vectors")
            progress["vector_cache"] = {
                "cache": str(self.vector_cache_path),
                "created_at": payload.get("created_at", ""),
                "vectors": len(vectors) if isinstance(vectors, dict) else 0,
                "case_count": payload.get("case_count", 0),
                "model": payload.get("model", ""),
            }
        except Exception as exc:
            progress["vector_cache"] = {"cache": str(self.vector_cache_path), "error": repr(exc)}
        return progress

    def rebuild_converted_markdown_manifest(self) -> dict[str, object]:
        started = time.time()
        payload = self._build_converted_markdown_manifest()
        self.converted_markdown_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.converted_markdown_manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._extra_search_texts, self._converted_markdown_status = self._read_converted_markdown_manifest()
        self._doc_frequency = self._build_doc_frequency()
        return {
            "ok": True,
            "cases": len(self._extra_search_texts),
            "documents": payload.get("document_count", 0),
            "skipped": payload.get("skipped_count", 0),
            "conflicts": payload.get("conflict_count", 0),
            "cache": str(self.converted_markdown_manifest_path),
            "seconds": round(time.time() - started, 2),
        }

    def _load_reusable_vectors(self, expected_hashes: dict[str, str]) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.vector_cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        if payload.get("model") != self.embedding_model:
            return {}
        vectors = payload.get("vectors")
        if not isinstance(vectors, dict):
            return {}
        reusable: dict[str, dict[str, object]] = {}
        for case_id, expected_hash in expected_hashes.items():
            item = vectors.get(case_id)
            if not isinstance(item, dict):
                continue
            if item.get("text_hash") != expected_hash or not isinstance(item.get("vector"), list):
                continue
            reusable[case_id] = item
        return reusable

    def _write_vector_cache(self, vectors: dict[str, dict[str, object]]) -> None:
        payload = {
            "schema_version": 2,
            "model": self.embedding_model,
            "embedding_url": self.embedding_url,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "registry_path": str(self.registry_path),
            "registry_hash": self._registry_hash(),
            "case_count": len(self._registry),
            "vectors": vectors,
        }
        self.vector_cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.vector_cache_path.with_suffix(self.vector_cache_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.vector_cache_path)

    def _write_reindex_progress(self, payload: dict[str, object]) -> None:
        self.reindex_progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.reindex_progress_path.with_suffix(self.reindex_progress_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.reindex_progress_path)

    def similar_cases(self, query: str, limit: int = 8) -> dict[str, object]:
        query_rewrite, rewrite_warnings = self._rewrite_query(query)
        search_queries = self._search_queries(query, query_rewrite)
        candidates = self._candidate_cases(search_queries, limit=max(limit * 20, 200))
        candidates = self._rerank_cases(query, candidates, limit=max(limit * 4, 32))
        cases = []
        sources = []
        for item in candidates[:limit]:
            case = dict(item["case"])
            evidence = self._case_evidence(case.get("case_id", ""), query, max_docs=3)
            quality_warnings = [str(doc.get("quality_warning")) for doc in evidence if doc.get("quality_warning")]
            case_payload = {
                "case_id": case.get("case_id", ""),
                "case_id_raw": case.get("case_id_raw", ""),
                "customer": case.get("customer", ""),
                "aircraft_type": case.get("aircraft_type", ""),
                "serial_or_registration": case.get("serial_or_registration", ""),
                "received_date": case.get("received_date", ""),
                "status_normalized": case.get("status_normalized", ""),
                "quote_sent_date": case.get("quote_sent_date", ""),
                "request_description": case.get("request_description", ""),
                "workscope_type": case.get("workscope_type", ""),
                "discipline_primary": case.get("discipline_primary", ""),
                "certificate_scope_flag": case.get("certificate_scope_flag", ""),
                "score": float(item.get("score", 0.0)),
                "semantic_score": float(item.get("semantic_score", 0.0)),
                "lexical_score": float(item.get("lexical_score", 0.0)),
                "exact_score": float(item.get("exact_score", 0.0)),
                "rerank_score": float(item.get("rerank_score", 0.0)),
                "matched_queries": item.get("matched_queries", []),
                "reasons": item.get("reasons", []),
                "check": self._check_points(query, case, evidence),
                "documents": evidence,
                "quality_warnings": quality_warnings,
            }
            cases.append(case_payload)
            for doc in evidence:
                if doc.get("source_type") != "commercial_offer_document":
                    continue
                sources.append(
                    {
                        "title": f"{case_payload['case_id']} / {doc.get('document_id') or Path(str(doc.get('path'))).name}",
                        "snippet": doc.get("snippet", ""),
                        "source_type": "commercial_offer_document",
                        "source_descriptor": {
                            "case_id": case_payload["case_id"],
                            "document_id": doc.get("document_id", ""),
                            "path": doc.get("path", ""),
                            "link": doc.get("link", ""),
                            "link_status": doc.get("link_status", ""),
                            "quality_warning": doc.get("quality_warning", ""),
                            "retrieval_mode": "commercial_offer_similarity",
                            "score": case_payload["score"],
                        },
                    }
                )
        answer = self._build_answer(query, cases, sources)
        llm_status = "retrieval_only"
        if self._llm is not None and cases:
            try:
                llm_answer = self._generate_answer(query, cases)
                if llm_answer.strip():
                    answer = f"{llm_answer}\n\n{self._sources_table(sources)}"
                    llm_status = "openai_compatible"
            except Exception:
                llm_status = "retrieval_fallback"
        warnings = list(self._index_status.get("warnings") or [])
        warnings.extend(rewrite_warnings)
        if not cases:
            warnings.append("no_similar_commercial_cases_found")
        return {
            "answer": answer,
            "sources": sources,
            "similar_cases": cases,
            "query_rewrite": query_rewrite,
            "warnings": warnings,
            "llm_status": llm_status,
        }

    def _rewrite_query(self, query: str) -> tuple[dict[str, object], list[str]]:
        empty = {
            "normalized_query": query.strip(),
            "search_variants": [],
            "key_terms": [],
            "workscope_hints": [],
            "component_hints": [],
            "zone_hints": [],
            "identifier_hints": exact_terms(query),
        }
        if not self.query_rewrite_enabled:
            return empty, []
        if self._llm is None:
            return empty, ["query_rewrite_unavailable"]
        system_prompt = (
            "Ты нормализуешь короткие запросы для поиска похожих коммерческих MRO-заявок. "
            "Верни только JSON без markdown. Не придумывай номера заявок, цены, сроки, документы или решения. "
            "Можно добавлять русские и английские авиационные эквиваленты, аббревиатуры и стандартные формулировки работ. "
            "Нельзя возвращать пустые массивы, если в запросе есть смысловые термины; search_variants должен содержать 3-8 вариантов. "
            "Раскрывай авиационные аббревиатуры и стандартные MRO-формулировки только на уровне терминов, без выбора номера заявки. "
            "Тип ВС не является фактором похожести; если он есть, не делай его главным поисковым признаком."
        )
        user_prompt = (
            "Сформируй JSON строго такой формы:\n"
            "{\n"
            '  "normalized_query": "одна короткая нормализованная формулировка",\n'
            '  "search_variants": ["3-8 альтернативных поисковых формулировок"],\n'
            '  "key_terms": ["важные термины"],\n'
            '  "workscope_hints": ["тип работы"],\n'
            '  "component_hints": ["компоненты"],\n'
            '  "zone_hints": ["зоны/позиции"],\n'
            '  "identifier_hints": ["AD/SB/FR/RIB/P/N/MSN/REG если явно есть"]\n'
            "}\n\n"
            "Требования:\n"
            "- normalized_query не должен быть простой копией запроса, если можно добавить MRO-термины.\n"
            "- search_variants: минимум 3 непустых варианта, включая русские и английские формулировки.\n"
            "- key_terms/component_hints/workscope_hints заполни важными словами из запроса и их эквивалентами.\n"
            "- identifier_hints заполняй только явными или стандартно нормализуемыми идентификаторами, без номеров заявок.\n\n"
            f"Запрос: {query.strip()}"
        )
        try:
            payload = self._parse_rewrite_json(self._llm.chat(system_prompt, user_prompt, allow_reasoning_fallback=True))
        except Exception:
            return empty, ["query_rewrite_unavailable"]
        normalized = normalize_spaces(str(payload.get("normalized_query") or query))
        warnings: list[str] = []
        rewrite = {
            "normalized_query": normalized or query.strip(),
            "search_variants": self._string_list(payload.get("search_variants"), limit=8),
            "key_terms": self._string_list(payload.get("key_terms"), limit=16),
            "workscope_hints": self._string_list(payload.get("workscope_hints"), limit=12),
            "component_hints": self._string_list(payload.get("component_hints"), limit=12),
            "zone_hints": self._string_list(payload.get("zone_hints"), limit=12),
            "identifier_hints": self._string_list(payload.get("identifier_hints"), limit=12) or exact_terms(query),
        }
        useful_terms = sum(
            len(rewrite.get(key) or [])
            for key in ("search_variants", "key_terms", "workscope_hints", "component_hints", "zone_hints", "identifier_hints")
        )
        if useful_terms == 0 and normalize_lookup(str(rewrite.get("normalized_query") or "")) == normalize_lookup(query):
            warnings.append("query_rewrite_empty")
        return rewrite, warnings

    @staticmethod
    def _parse_rewrite_json(text: str) -> dict[str, object]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        decoder = json.JSONDecoder()
        objects: list[dict[str, object]] = []
        for match in re.finditer(r"\{", cleaned):
            try:
                payload, _ = decoder.raw_decode(cleaned[match.start() :])
            except ValueError:
                continue
            if isinstance(payload, dict):
                objects.append(payload)
        if not objects:
            variants = CommercialOffersService._rewrite_variants_from_text(cleaned)
            if variants:
                return {
                    "normalized_query": variants[0],
                    "search_variants": variants,
                    "key_terms": [],
                    "workscope_hints": [],
                    "component_hints": [],
                    "zone_hints": [],
                    "identifier_hints": [],
                }
            raise ValueError("query rewrite JSON must be an object")
        return objects[-1]

    @staticmethod
    def _rewrite_variants_from_text(text: str) -> list[str]:
        variants: list[str] = []
        seen: set[str] = set()
        patterns = [
            r'"([^"\n]{8,160})"',
            r"`([^`\n]{8,160})`",
            r"(?:^|\n)\s*(?:\d+\.|[-*])\s+([^\n]{8,160})",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                value = normalize_spaces(match.group(1))
                value = re.sub(r"\s+\([^)]*\)\s*$", "", value).strip()
                if not value or value.startswith("{") or ":" in value[:24]:
                    continue
                key = normalize_lookup(value)
                if key in seen:
                    continue
                seen.add(key)
                variants.append(value)
                if len(variants) >= 8:
                    return variants
        return variants

    @staticmethod
    def _string_list(value: object, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = normalize_spaces(str(item or ""))
            key = normalize_lookup(text)
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text[:240])
            if len(result) >= limit:
                break
        return result

    def _search_queries(self, query: str, rewrite: dict[str, object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = [{"text": query.strip(), "source": "original_query", "weight": 1.0}]
        normalized = normalize_spaces(str(rewrite.get("normalized_query") or ""))
        if normalized and normalize_lookup(normalized) != normalize_lookup(query):
            items.append({"text": normalized, "source": "normalized_query", "weight": 0.96})
        for value in rewrite.get("search_variants") or []:
            items.append({"text": str(value), "source": "search_variant", "weight": 0.9})
        for field, weight in (
            ("key_terms", 0.82),
            ("workscope_hints", 0.78),
            ("component_hints", 0.78),
            ("zone_hints", 0.78),
            ("identifier_hints", 0.88),
        ):
            values = [str(value) for value in rewrite.get(field) or [] if str(value).strip()]
            if values:
                items.append({"text": " ".join(values), "source": field, "weight": weight})
        deduped: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in items:
            text = normalize_spaces(str(item.get("text") or ""))
            key = normalize_lookup(text)
            if not text or key in seen:
                continue
            if item.get("source") != "original_query" and not self._is_informative_rewrite_variant(text):
                continue
            seen.add(key)
            deduped.append({**item, "text": text})
        return deduped[:14]

    @staticmethod
    def _is_informative_rewrite_variant(text: str) -> bool:
        tokens = [token for token in tokenize(text) if token.replace("-", "") not in aircraft_tokens(text)]
        if exact_terms(text):
            return True
        if len(tokens) < 2:
            return False
        long_or_latin = [token for token in tokens if len(token) >= 5 or re.search(r"[a-z]", token)]
        return len(long_or_latin) >= 2

    def _load_registry(self) -> list[dict[str, str]]:
        if not self.registry_path.exists():
            return []
        with self.registry_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _load_links(self) -> dict[str, dict[str, object]]:
        links: dict[str, dict[str, object]] = {}
        if not self.links_path.exists():
            return links
        with self.links_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                case_id = row.get("case_id", "")
                if not case_id:
                    continue
                docs = [self._resolve_document_path(item.strip()) for item in row.get("documents", "").split(";") if item.strip()]
                links[case_id] = {
                    "link_status": row.get("link_status", ""),
                    "document_count": row.get("document_count", ""),
                    "documents": docs,
                }
        return links

    def _load_documents(self) -> dict[str, dict[str, object]]:
        documents: dict[str, dict[str, object]] = {}
        if not self.documents_path.exists():
            return documents
        with self.documents_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                path = self._resolve_document_path(str(payload.get("normalized_md") or ""))
                if path:
                    documents[str(path)] = payload
        return documents

    def _read_converted_markdown_manifest(self) -> tuple[dict[str, str], dict[str, object]]:
        warnings: list[str] = []
        status: dict[str, object] = {"stale": False, "warnings": warnings}
        try:
            payload = json.loads(self.converted_markdown_manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            warnings.append("commercial_offer_converted_markdown_manifest_missing_run_reindex_com_offers")
            status["stale"] = True
            return {}, status
        except (OSError, ValueError) as exc:
            warnings.append(f"commercial_offer_converted_markdown_manifest_unreadable: {exc}")
            status["stale"] = True
            return {}, status
        if payload.get("schema_version") != CONVERTED_MARKDOWN_SCHEMA_VERSION:
            warnings.append("commercial_offer_converted_markdown_manifest_schema_mismatch_run_reindex_com_offers")
            status["stale"] = True
            return {}, status
        cases = payload.get("cases")
        if not isinstance(cases, dict):
            warnings.append("commercial_offer_converted_markdown_manifest_has_no_cases")
            status["stale"] = True
            return {}, status
        status["created_at"] = payload.get("created_at", "")
        status["documents"] = payload.get("document_count", 0)
        status["skipped"] = payload.get("skipped_count", 0)
        status["conflicts"] = payload.get("conflict_count", 0)
        texts: dict[str, str] = {}
        for case_id, item in cases.items():
            if isinstance(item, dict):
                text = str(item.get("search_text") or "")
            else:
                text = str(item or "")
            if text:
                texts[str(case_id)] = text
        return texts, status

    def _build_converted_markdown_manifest(self) -> dict[str, object]:
        by_case: dict[str, list[dict[str, object]]] = {}
        skipped: list[dict[str, str]] = []
        conflicts: list[dict[str, str]] = []
        roots = [self.root / "converted_md", self.root / "converted_md_pdf_ocr"]
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                case_id, conflict = self._case_id_from_markdown_path(path, root)
                rel = str(path.relative_to(self.root))
                if self._should_skip_converted_markdown_path(path, root):
                    skipped.append({"path": rel, "reason": "excluded_path"})
                    continue
                if conflict:
                    conflicts.append({"path": rel, "reason": conflict})
                    continue
                if not case_id.startswith("MP-"):
                    skipped.append({"path": rel, "reason": "case_id_not_found"})
                    continue
                docs = by_case.setdefault(case_id, [])
                if len(docs) >= 4:
                    skipped.append({"path": rel, "reason": "case_document_limit"})
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    text = ""
                search_text = self._extract_markdown_search_text(text, rel)
                if not search_text:
                    skipped.append({"path": rel, "reason": "empty_search_text"})
                    continue
                docs.append({"path": rel, "search_text": search_text})
        cases = {}
        for case_id, docs in by_case.items():
            combined = "\n\n".join(f"path: {doc['path']}\n{doc['search_text']}" for doc in docs)
            cases[case_id] = {"search_text": combined[:12000], "documents": docs}
        return {
            "schema_version": CONVERTED_MARKDOWN_SCHEMA_VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "roots": [str(root) for root in roots],
            "case_count": len(cases),
            "document_count": sum(len(docs) for docs in by_case.values()),
            "skipped_count": len(skipped),
            "conflict_count": len(conflicts),
            "skipped": skipped[:200],
            "conflicts": conflicts[:200],
            "cases": cases,
        }

    @staticmethod
    def _case_id_from_markdown_path(path: Path, root: Path) -> tuple[str, str]:
        relative_parts = path.relative_to(root).parts
        case_dir_ids = []
        for part in relative_parts[:-1]:
            if RANGE_DIR_RE.match(part):
                continue
            match = CASE_RE.search(part)
            if match:
                case_dir_ids.append(normalize_case_id(match.group(0)))
        file_ids = [normalize_case_id(match.group(0)) for match in CASE_RE.finditer(path.name)]
        ids = case_dir_ids + file_ids
        unique_ids = []
        for case_id in ids:
            if case_id not in unique_ids:
                unique_ids.append(case_id)
        if not unique_ids:
            return "", ""
        if len(unique_ids) > 1:
            return "", f"conflicting_case_ids:{','.join(unique_ids)}"
        return unique_ids[0], ""

    @staticmethod
    def _should_skip_converted_markdown_path(path: Path, root: Path) -> bool:
        parts = [normalize_lookup(part) for part in path.relative_to(root).parts]
        if any(part in CONVERTED_MARKDOWN_EXCLUDED_PARTS for part in parts):
            return True
        rel = normalize_lookup(str(path.relative_to(root)))
        return bool(CONVERTED_MARKDOWN_SKIP_RE.search(rel))

    @staticmethod
    def _extract_markdown_search_text(text: str, relative_path: str) -> str:
        lines = [normalize_spaces(line.lstrip("#> -*\t")) for line in text.splitlines()]
        selected: list[str] = []
        seen: set[str] = set()

        def add(line: str) -> None:
            clean = normalize_spaces(line)
            if len(clean) < 4:
                return
            if clean.startswith("<!--") or clean.lower().startswith("source pdf:"):
                return
            key = normalize_lookup(clean)
            if key in seen:
                return
            seen.add(key)
            selected.append(clean[:500])

        add(Path(relative_path).stem.replace("_", " "))
        for line in lines[:80]:
            if line:
                add(line)
            if len(selected) >= 12:
                break
        keyword_re = re.compile(
            r"запрос|описани|поврежден|трещ|корроз|ремонт|решени|problem|request|description|desired|solution|repair|damage|crack|corrosion|amoc",
            re.IGNORECASE,
        )
        for line in lines:
            if keyword_re.search(line) or exact_terms(line):
                add(line)
            if len(selected) >= 40:
                break
        return "\n".join(selected)[:6000]

    def _resolve_document_path(self, value: str) -> str:
        if not value:
            return ""
        marker = "normalized_md/"
        if marker in value:
            suffix = value.split(marker, 1)[1]
            for candidate in (
                self.artifacts / "normalized_md" / suffix,
                self.root / "converted_md_pdf_ocr" / suffix,
            ):
                if candidate.exists():
                    return str(candidate)
        path = Path(value)
        if path.exists():
            return str(path)
        return value

    def _case_similarity_text(self, row: dict[str, str]) -> str:
        parts = []
        for key in (
            "case_id",
            "case_id_raw",
            "customer",
            "serial_or_registration",
            "request_description",
            "bd_comments",
            "tasks",
            "notes",
            "workscope_type",
            "discipline_primary",
            "status_normalized",
            "certificate_scope_flag",
            "similar_case_refs",
        ):
            parts.append(f"{key}: {strip_aircraft_terms(row.get(key, ''))}")
        extra_text = self._extra_search_texts.get(row.get("case_id", ""), "")
        if extra_text:
            parts.append(f"converted_markdown: {strip_aircraft_terms(extra_text)}")
        reestr_text = self._reestr_enrichment.get(row.get("case_id", ""), "")
        if reestr_text:
            parts.append(f"reestr_zayavok: {strip_aircraft_terms(reestr_text)}")
        return "\n".join(parts)

    def _load_reestr_enrichment(self) -> tuple[dict[str, str], dict[str, object]]:
        warnings: list[str] = []
        status: dict[str, object] = {"stale": False, "warnings": warnings}
        if not self.reestr_path.exists():
            warnings.append("reestr_zayavok_missing")
            status["stale"] = True
            return {}, status
        try:
            rows = self._read_xlsx_sheet(self.reestr_path, "Реестр")
        except Exception as exc:
            warnings.append(f"reestr_zayavok_unreadable: {exc}")
            status["stale"] = True
            return {}, status
        by_case: dict[str, list[str]] = {}
        for row_number, row in rows:
            if row_number < 5:
                continue
            case_id = normalize_case_id(row.get("D", ""))
            if not case_id.startswith("MP-"):
                continue
            parts = []
            for label, column in (
                ("reestr_description", "I"),
                ("reestr_bd_comments", "P"),
                ("reestr_tasks", "Q"),
                ("reestr_notes", "AA"),
                ("reestr_folder", "AF"),
            ):
                value = normalize_spaces(row.get(column, ""))
                if not value or value in {"-", "#N/A"}:
                    continue
                parts.append(f"{label}: {value}")
            if parts:
                by_case.setdefault(case_id, []).append("\n".join(parts))
        status["rows"] = len(rows)
        return {case_id: "\n".join(values)[:8000] for case_id, values in by_case.items()}, status

    @staticmethod
    def _read_xlsx_sheet(path: Path, sheet_name: str) -> list[tuple[int, dict[str, str]]]:
        with zipfile.ZipFile(path) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall("a:si", SPREADSHEET_NAMESPACES):
                    shared_strings.append("".join(text.text or "" for text in item.iter(f"{{{SPREADSHEET_NS}}}t")))
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationships = {rel.attrib["Id"]: rel.attrib["Target"].lstrip("/") for rel in relroot}
            sheet_nodes = workbook.find("a:sheets", SPREADSHEET_NAMESPACES)
            if sheet_nodes is None:
                return []
            target = ""
            for sheet in sheet_nodes:
                if sheet.attrib.get("name") != sheet_name:
                    continue
                relationship_id = sheet.attrib[f"{{{SPREADSHEET_REL_NS}}}id"]
                target = relationships[relationship_id]
                break
            if not target:
                return []
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            root = ET.fromstring(archive.read(target))
            rows = []
            for row_node in root.findall(".//a:sheetData/a:row", SPREADSHEET_NAMESPACES):
                values: dict[str, str] = {}
                for cell in row_node.findall("a:c", SPREADSHEET_NAMESPACES):
                    value_node = cell.find("a:v", SPREADSHEET_NAMESPACES)
                    value = "" if value_node is None else value_node.text or ""
                    if cell.attrib.get("t") == "s" and value:
                        value = shared_strings[int(value)]
                    values[CommercialOffersService._xlsx_column_name(cell.attrib.get("r", ""))] = value.strip()
                if any(value.strip() for value in values.values()):
                    rows.append((int(row_node.attrib.get("r", "0")), values))
            return rows

    @staticmethod
    def _xlsx_column_name(cell_ref: str) -> str:
        match = re.match(r"([A-Z]+)", cell_ref or "")
        return match.group(1) if match else ""

    def _candidate_cases(self, search_queries: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        merged: dict[str, dict[str, object]] = {}
        for query_item in search_queries:
            query = str(query_item.get("text") or "")
            query_source = str(query_item.get("source") or "query")
            weight = float(query_item.get("weight") or 1.0)
            source_results = {
                "semantic": self._semantic_candidate_cases(query, limit=limit),
                "lexical": self._lexical_candidate_cases(query, limit=limit),
                "exact": self._exact_candidate_cases(query, limit=limit),
            }
            source_max: dict[str, float] = {}
            source_min: dict[str, float] = {}
            for source_name, items in source_results.items():
                scores = [float(item.get("score", 0.0)) for item in items if float(item.get("score", 0.0)) > 0]
                source_max[source_name] = max(scores) if scores else 0.0
                source_min[source_name] = min(scores) if scores else 0.0
            for source_name, items in (
                ("semantic", source_results["semantic"]),
                ("lexical", source_results["lexical"]),
                ("exact", source_results["exact"]),
            ):
                for item in items:
                    case = item["case"]
                    case_id = str(case.get("case_id") or "")
                    if not case_id:
                        continue
                    current = merged.get(case_id)
                    if current is None:
                        current = {
                            "case": case,
                            "score": 0.0,
                            "semantic_score": 0.0,
                            "lexical_score": 0.0,
                            "exact_score": 0.0,
                            "reasons": [],
                            "retrieval_modes": [],
                            "matched_queries": [],
                            "normalized_scores": {},
                        }
                        merged[case_id] = current
                    raw_score = float(item.get("score", 0.0))
                    score = raw_score * weight
                    current[f"{source_name}_score"] = max(float(current.get(f"{source_name}_score", 0.0)), score)
                    normalized = self._normalize_source_score(raw_score, source_min[source_name], source_max[source_name]) * weight
                    normalized_scores = current.get("normalized_scores")
                    if isinstance(normalized_scores, dict):
                        normalized_scores[source_name] = max(float(normalized_scores.get(source_name, 0.0)), normalized)
                    current["retrieval_modes"].append(source_name)
                    matched_queries = current["matched_queries"]
                    if isinstance(matched_queries, list):
                        marker = f"{query_source}: {query}"
                        if marker not in matched_queries:
                            matched_queries.append(marker)
                    reasons = current["reasons"]
                    if isinstance(reasons, list):
                        prefix = "по исходному запросу" if query_source == "original_query" else "по расширенному запросу"
                        for reason in item.get("reasons", []):
                            labeled = f"{prefix}: {reason}"
                            if reason and labeled not in reasons:
                                reasons.append(labeled)
        candidates = []
        for item in merged.values():
            semantic = float(item.get("semantic_score", 0.0))
            lexical = float(item.get("lexical_score", 0.0))
            exact = float(item.get("exact_score", 0.0))
            normalized_scores = item.get("normalized_scores") if isinstance(item.get("normalized_scores"), dict) else {}
            score = (
                float(normalized_scores.get("semantic", 0.0))
                + float(normalized_scores.get("lexical", 0.0))
                + float(normalized_scores.get("exact", 0.0))
            )
            matched_queries = item.get("matched_queries") if isinstance(item.get("matched_queries"), list) else []
            score += min(len(matched_queries), 5) * 0.035
            if score > 0:
                original_query = str(search_queries[0].get("text") or "") if search_queries else ""
                score += self._aircraft_tie_breaker(original_query, item["case"])
            item["score"] = score
            item["reasons"] = item.get("reasons", [])[:12]
            item["matched_queries"] = matched_queries[:8]
            candidates.append(item)
        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        return candidates[:limit]

    @staticmethod
    def _normalize_source_score(score: float, source_min: float, source_max: float) -> float:
        if score <= 0:
            return 0.0
        if source_max <= source_min:
            return 1.0
        return max(0.0, min(1.0, (score - source_min) / (source_max - source_min)))

    def _exact_candidate_cases(self, query: str, limit: int) -> list[dict[str, object]]:
        terms = exact_terms(query)
        explicit_case_id = normalize_case_id(query)
        candidates = []
        if not terms and explicit_case_id == query.strip():
            return candidates
        for row in self._registry:
            haystack = normalize_lookup(self._case_similarity_text(row))
            score = 0.0
            reasons: list[str] = []
            if explicit_case_id and row.get("case_id") == explicit_case_id:
                score += 100.0
                reasons.append("точное совпадение номера заявки")
            for term in terms:
                normalized = normalize_lookup(term).replace(" ", "")
                compact_haystack = haystack.replace(" ", "")
                if normalized and normalized in compact_haystack:
                    score += 8.0 if len(normalized) >= 5 else 5.0
                    reasons.append(f"точный термин: {term}")
            if score > 0:
                candidates.append({"case": row, "score": score, "reasons": reasons})
        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        return candidates[:limit]

    @staticmethod
    def _aircraft_tie_breaker(query: str, row: dict[str, str]) -> float:
        query_aircraft = aircraft_tokens(query)
        case_aircraft = aircraft_tokens(row.get("aircraft_type", ""))
        if not query_aircraft or not case_aircraft:
            return 0.0
        for query_token in query_aircraft:
            for case_token in case_aircraft:
                if query_token == case_token or case_token.startswith(query_token) or query_token.startswith(case_token):
                    return 0.015
        return 0.0

    def _lexical_candidate_cases(self, query: str, limit: int) -> list[dict[str, object]]:
        ignored_aircraft_tokens = aircraft_tokens(query)
        query_tokens = Counter(token for token in tokenize(query) if token.replace("-", "") not in ignored_aircraft_tokens)
        if not query_tokens:
            return []
        total_docs = max(1, len(self._registry))
        candidates = []
        for row in self._registry:
            text = self._case_similarity_text(row)
            row_tokens = Counter(tokenize(text))
            score = 0.0
            reasons: list[str] = []
            for token, count in query_tokens.items():
                frequency = row_tokens.get(token, 0)
                if not frequency:
                    continue
                idf = math.log((total_docs + 1) / (1 + self._doc_frequency.get(token, 0))) + 1.0
                field_bonus = 1.0
                if token in tokenize(row.get("request_description", "")):
                    field_bonus += 1.6
                if token in tokenize(self._extra_search_texts.get(row.get("case_id", ""))):
                    field_bonus += 1.8
                if token in tokenize(row.get("serial_or_registration", "")):
                    field_bonus += 1.0
                score += idf * (1.0 + math.log(frequency)) * count * field_bonus
                reasons.append(f"ключевое слово: {token}")
            phrase_score = self._phrase_overlap_score(
                query,
                " ".join([row.get("request_description", ""), self._extra_search_texts.get(row.get("case_id", "")) or ""]),
            )
            if phrase_score:
                score += phrase_score
                reasons.append("совпадение фразы в описании")
            if score > 0:
                candidates.append({"case": row, "score": score, "reasons": reasons[:12]})
        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        return candidates[:limit]

    @staticmethod
    def _phrase_overlap_score(query: str, description: str) -> float:
        query_tokens = tokenize(strip_aircraft_terms(query))
        if len(query_tokens) < 2:
            return 0.0
        haystack = f" {' '.join(tokenize(strip_aircraft_terms(description)))} "
        score = 0.0
        seen: set[str] = set()
        for size, weight, cap in ((4, 4.0, 12.0), (3, 2.3, 9.2), (2, 0.9, 4.5)):
            subtotal = 0.0
            for idx in range(0, len(query_tokens) - size + 1):
                phrase = " ".join(query_tokens[idx : idx + size])
                if phrase in seen:
                    continue
                seen.add(phrase)
                if f" {phrase} " in haystack:
                    subtotal += weight
            score += min(subtotal, cap)
        return score

    def _semantic_candidate_cases(self, query: str, limit: int) -> list[dict[str, object]]:
        if not self._vectors:
            return []
        query_vector = self._embed_text(strip_aircraft_terms(query))
        if not query_vector:
            return []
        ignored_aircraft_tokens = aircraft_tokens(query)
        query_tokens = {token for token in tokenize(query) if token.replace("-", "") not in ignored_aircraft_tokens}
        candidates = []
        for case_id, item in self._vectors.items():
            row = self._registry_by_case.get(case_id)
            vector = item.get("vector") if isinstance(item, dict) else None
            if not row or not isinstance(vector, list):
                continue
            score = self._cosine(query_vector, [float(value) for value in vector])
            row_tokens = set(tokenize(self._case_similarity_text(row)))
            reasons = [f"общий термин: {token}" for token in sorted(query_tokens.intersection(row_tokens), key=lambda token: (-len(token), token))[:12]]
            candidates.append({"case": row, "score": score, "reasons": reasons, "semantic_score": score})
        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        return candidates[:limit]

    def _read_vector_cache(self) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        warnings: list[str] = []
        status: dict[str, object] = {"stale": False, "warnings": warnings}
        try:
            payload = json.loads(self.vector_cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            warnings.append("commercial_offer_embedding_index_missing_run_reindex_com_offers")
            status["stale"] = True
            return {}, status
        except (OSError, ValueError) as exc:
            warnings.append(f"commercial_offer_embedding_index_unreadable: {exc}")
            status["stale"] = True
            return {}, status
        status["created_at"] = payload.get("created_at", "")
        if payload.get("model") != self.embedding_model:
            warnings.append("commercial_offer_embedding_index_model_mismatch_run_reindex_com_offers")
            status["stale"] = True
            return {}, status
        vectors = payload.get("vectors")
        if not isinstance(vectors, dict):
            warnings.append("commercial_offer_embedding_index_has_no_vectors")
            status["stale"] = True
            return {}, status
        registry_hash = payload.get("registry_hash")
        if registry_hash and registry_hash != self._registry_hash():
            warnings.append("commercial_offer_embedding_index_stale_registry_changed_run_reindex_com_offers")
            status["stale"] = True
            return {}, status
        by_case = {row.get("case_id", ""): self._text_hash(self._case_similarity_text(row)) for row in self._registry}
        missing = []
        valid_vectors: dict[str, dict[str, object]] = {}
        for case_id, text_hash in by_case.items():
            item = vectors.get(case_id)
            if not isinstance(item, dict) or item.get("text_hash") != text_hash or not isinstance(item.get("vector"), list):
                missing.append(case_id)
            else:
                valid_vectors[case_id] = item
        if missing:
            warnings.append("commercial_offer_embedding_index_incomplete_or_stale_run_reindex_com_offers")
            if valid_vectors:
                status["partial"] = True
                status["missing_count"] = len(missing)
                status["missing_cases"] = missing[:50]
                return valid_vectors, status
            status["stale"] = True
            return {}, status
        return valid_vectors, status

    def _embed_text(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.embedding_model, "prompt": text[:6000]}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.embedding_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        embedding = body.get("embedding")
        if not isinstance(embedding, list):
            return []
        return [float(value) for value in embedding]

    def _build_doc_frequency(self) -> Counter[str]:
        frequency: Counter[str] = Counter()
        for row in self._registry:
            frequency.update(set(tokenize(self._case_similarity_text(row))))
        return frequency

    def _registry_hash(self) -> str:
        digest = hashlib.sha256()
        if self.registry_path.exists():
            digest.update(self.registry_path.read_bytes())
        for row in self._registry:
            digest.update((row.get("case_id", "") + ":" + self._text_hash(self._case_similarity_text(row))).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.sha256(normalize_spaces(text).encode("utf-8")).hexdigest()

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    def _rerank_cases(self, query: str, candidates: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        if not candidates:
            return []
        if self._reranker is None:
            return candidates[:limit]
        rerank_query = strip_aircraft_terms(query)
        pairs = [[rerank_query, self._case_similarity_text(item["case"])] for item in candidates]
        try:
            scores = self._reranker.compute_score(pairs, normalize=True)
        except Exception:
            return candidates[:limit]
        rescored = []
        for item, score in zip(candidates, scores, strict=False):
            updated = dict(item)
            updated["score"] = float(item.get("score", 0.0)) * 0.18 + float(score)
            updated["rerank_score"] = float(score)
            rescored.append(updated)
        rescored.sort(key=lambda item: float(item["score"]), reverse=True)
        return rescored[:limit]

    def _case_evidence(self, case_id: str, query: str, max_docs: int) -> list[dict[str, object]]:
        link = self._links.get(case_id, {})
        link_status = str(link.get("link_status") or "missing_document")
        paths = [str(path) for path in link.get("documents", []) if path]
        if link_status == "missing_document" or not paths:
            return [
                {
                    "document_id": "",
                    "path": "",
                    "link": "",
                    "link_status": link_status,
                    "source_type": "missing_document",
                    "snippet": "Документы не найдены, похожесть рассчитана по реестру коммерческих заявок.",
                    "quality_warning": "документы не найдены",
                }
            ]
        if link_status != "matched":
            return [
                {
                    "document_id": "",
                    "path": "",
                    "link": "",
                    "link_status": link_status,
                    "source_type": "unverified_document_link",
                    "snippet": "Документная связь не подтверждена; документы не использованы как источники.",
                    "quality_warning": "связь документа требует проверки",
                }
            ]
        scored = []
        for path in paths:
            text = self._read_text(path)
            meta = self._documents.get(path, {})
            document_id = str(meta.get("document_id") or Path(path).stem)
            if not self._document_matches_case(case_id, path, document_id):
                continue
            score = self._document_score(query, path, text)
            if not text:
                scored.append(
                    {
                        "score": 0.0,
                        "document_id": document_id,
                        "path": path,
                        "link": "",
                        "link_status": "unreadable_document",
                        "source_type": "unverified_document_link",
                        "snippet": "Документ указан в связях, но файл не удалось прочитать; документ не использован как источник.",
                        "confidence": meta.get("confidence", ""),
                        "quality_warning": "документ недоступен или пуст",
                    }
                )
            else:
                scored.append(
                    {
                        "score": score,
                        "document_id": document_id,
                        "path": path,
                        "link": Path(path).as_uri() if Path(path).is_absolute() else path,
                        "link_status": link_status,
                        "source_type": "commercial_offer_document",
                        "snippet": self._focused_snippet(text, query),
                        "confidence": meta.get("confidence", ""),
                        "quality_warning": "",
                    }
                )
        if not scored:
            return [
                {
                    "document_id": "",
                    "path": "",
                    "link": "",
                    "link_status": "document_link_mismatch",
                    "source_type": "unverified_document_link",
                    "snippet": "Связанные документы относятся к другой заявке или не содержат проверяемого номера заявки; документы не использованы как источники.",
                    "quality_warning": "связь документа не совпадает с номером заявки",
                }
            ]
        scored.sort(key=lambda item: float(item["score"]), reverse=True)
        return scored[:max_docs]

    @staticmethod
    def _case_family(value: str) -> str:
        match = CASE_RE.search(value or "")
        if not match:
            return ""
        return str(int(match.group(1)))

    @classmethod
    def _document_matches_case(cls, case_id: str, path: str, document_id: str) -> bool:
        expected = cls._case_family(case_id)
        if not expected:
            return False
        haystack = f"{path}\n{document_id}"
        return any(cls._case_family(match.group(0)) == expected for match in CASE_RE.finditer(haystack))

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    @staticmethod
    def _document_score(query: str, path: str, text: str) -> float:
        haystack = normalize_lookup(f"{path}\n{text[:20000]}")
        compact = haystack.replace(" ", "")
        score = 0.0
        ignored_aircraft_tokens = aircraft_tokens(query)
        for token in (token for token in tokenize(query) if token.replace("-", "") not in ignored_aircraft_tokens):
            if token in haystack:
                score += 1.0
        for term in exact_terms(query):
            if normalize_lookup(term).replace(" ", "") in compact:
                score += 5.0
        return score

    @staticmethod
    def _focused_snippet(text: str, query: str, window: int = 1400) -> str:
        cleaned = normalize_spaces(text)
        if len(cleaned) <= window:
            return cleaned
        lowered = normalize_lookup(cleaned)
        positions = []
        ignored_aircraft_tokens = aircraft_tokens(query)
        for token in (token for token in tokenize(query) if token.replace("-", "") not in ignored_aircraft_tokens):
            pos = lowered.find(token)
            if pos >= 0:
                positions.append(pos)
        for term in exact_terms(query):
            pos = lowered.replace(" ", "").find(normalize_lookup(term).replace(" ", ""))
            if pos >= 0:
                positions.append(pos)
        if not positions:
            return cleaned[:window]
        start = max(min(positions) - window // 3, 0)
        return cleaned[start : start + window]

    def _check_points(self, query: str, case: dict[str, str], evidence: list[dict[str, object]]) -> list[str]:
        checks: list[str] = []
        query_aircraft = {normalize_lookup(item).replace(" ", "") for item in AIRCRAFT_RE.findall(query or "")}
        case_aircraft = normalize_lookup(case.get("aircraft_type", "")).replace(" ", "")
        if query_aircraft and case_aircraft and case_aircraft not in query_aircraft:
            checks.append(f"тип ВС отличается или требует проверки: {case.get('aircraft_type', '')}")
        status = case.get("status_normalized", "")
        if status and status != "accepted":
            checks.append(f"статус заявки: {status}")
        if any(doc.get("source_type") == "unverified_document_link" for doc in evidence):
            checks.append("документная связь не подтверждена; документы не использованы как источники")
        if any(doc.get("source_type") == "missing_document" for doc in evidence):
            checks.append("документы отсутствуют; проверять по реестру")
        return checks or ["сверить фактическую зону повреждения, применимость и исходные данные"]

    def _build_answer(self, query: str, cases: list[dict[str, object]], sources: list[dict[str, object]]) -> str:
        lines = [f"Описание для поиска аналогов: {query.strip()}", ""]
        if not cases:
            lines.append("Похожие коммерческие заявки не найдены.")
            lines.append("")
            lines.append("Это поиск аналогов, не расчет цены и не финальное решение брать/не брать.")
            return "\n".join(lines)
        lines.append("| Заявка | Заказчик | ВС | Статус | Описание | Почему похожа | Что проверить | Документы |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for case in cases:
            docs = case.get("documents") if isinstance(case.get("documents"), list) else []
            doc_links = []
            for doc in docs[:2]:
                title = str(doc.get("document_id") or doc.get("snippet") or "нет документа")
                link = str(doc.get("link") or "")
                status = str(doc.get("link_status") or "")
                warning = str(doc.get("quality_warning") or "")
                label = title
                if status:
                    label = f"{label} ({status})"
                if warning:
                    label = f"{label}: {warning}"
                doc_links.append(f"[{label}]({link})" if link else label)
            description = normalize_spaces(str(case.get("request_description") or ""))[:180].replace("|", "\\|")
            reasons = "; ".join(str(reason) for reason in case.get("reasons", [])[:8]).replace("|", "\\|")
            checks = "; ".join(str(item) for item in case.get("check", [])[:4]).replace("|", "\\|")
            lines.append(
                f"| {case.get('case_id', '')} | {case.get('customer', '')} | {case.get('aircraft_type', '')} | "
                f"{case.get('status_normalized', '')} | {description} | {reasons} | {checks} | {'<br>'.join(doc_links)} |"
            )
        lines.append("")
        lines.append("Предупреждения по качеству источников:")
        warnings = sorted({str(w) for case in cases for w in case.get("quality_warnings", []) if w})
        if warnings:
            for warning in warnings:
                lines.append(f"- {warning}")
        else:
            lines.append("- Документные связи без специальных предупреждений.")
        lines.append("")
        lines.append("Это поиск аналогов, не расчет цены и не финальное решение брать/не брать.")
        lines.append("")
        lines.append(self._sources_table(sources))
        return "\n".join(lines)

    def _generate_answer(self, query: str, cases: list[dict[str, object]]) -> str:
        assert self._llm is not None
        system_prompt = (
            "Ты помощник по поиску аналогичных коммерческих MRO-заявок. "
            "Работай только с переданными кандидатами из com_offers/case_registry.csv и evidence layer. "
            "Не используй знания вне источников и не придумывай цену, часы или решение. "
            "Ответ всегда должен содержать таблицу: case_id, заказчик, тип ВС, статус, описание, почему похожа, что отличается/что проверить, документы. "
            "Используй документы как источники только если они переданы с link_status=matched. "
            "Для ambiguous_match или document_link_mismatch явно пиши, что документы не использованы как источники. "
            "Для missing_document пиши: документы не найдены, похожесть рассчитана по реестру. "
            "В конце добавь: это поиск аналогов, не расчет цены и не финальное решение брать/не брать."
        )
        lines = [f"Новая заявка или описание:\n{query.strip()}\n", "Кандидаты:"]
        for case in cases:
            lines.append(
                "\n"
                f"case_id={case.get('case_id')}\n"
                f"customer={case.get('customer')}\n"
                f"aircraft_type={case.get('aircraft_type')}\n"
                f"status={case.get('status_normalized')}\n"
                f"description={case.get('request_description')}\n"
                f"reasons={case.get('reasons')}\n"
                f"check={case.get('check')}\n"
            )
            for doc in (case.get("documents") or [])[:2]:
                if doc.get("source_type") != "commercial_offer_document":
                    lines.append(
                        f"document_status={doc.get('link_status')}\n"
                        f"quality_warning={doc.get('quality_warning')}\n"
                        f"note={doc.get('snippet')}\n"
                    )
                    continue
                lines.append(
                    f"document={doc.get('document_id')}\n"
                    f"link_status={doc.get('link_status')}\n"
                    f"quality_warning={doc.get('quality_warning')}\n"
                    f"link={doc.get('link')}\n"
                    f"snippet={str(doc.get('snippet') or '')[:900]}\n"
                )
        return self._llm.chat(system_prompt, "\n".join(lines))

    @staticmethod
    def _sources_table(sources: list[dict[str, object]]) -> str:
        if not sources:
            return ""
        rows = ["### Источники", "", "| Заявка | Документ | Статус связи | Фрагмент | Ссылка |", "|---|---|---|---|---|"]
        for source in sources[:10]:
            descriptor = source.get("source_descriptor") or {}
            snippet = normalize_spaces(str(source.get("snippet") or ""))
            if len(snippet) > 220:
                snippet = snippet[:217] + "..."
            snippet = snippet.replace("|", "\\|")
            link = str(descriptor.get("link") or "")
            title = str(descriptor.get("document_id") or "")
            status = str(descriptor.get("link_status") or "")
            warning = str(descriptor.get("quality_warning") or "")
            if warning:
                status = f"{status}: {warning}" if status else warning
            rows.append(
                f"| {descriptor.get('case_id', '')} | {title} | {status} | {snippet} | "
                f"{f'[Открыть]({link})' if link else ''} |"
            )
        return "\n".join(rows)

    def _link_status_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for link in self._links.values():
            counts[str(link.get("link_status") or "unknown")] += 1
        return dict(counts)
