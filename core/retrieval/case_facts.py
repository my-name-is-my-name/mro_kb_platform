from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Protocol

from core.models.entities import (
    CaseFactsRequest,
    CaseFactsResponse,
    CaseResolution,
    CorpusSummary,
    HistoricalFact,
    HistoricalReference,
)
from core.retrieval.vector import MroQdrantIndex
from core.runtime_clients import OpenAICompatibleLLM, RuntimeSettings
from storage.sqlite.store import SQLiteStore


CATEGORY_QUERIES = {
    "problem": "damage повреждение crack трещина corrosion коррозия defect зона",
    "activity": "assessment оценка repair ремонт inspection инспекция analysis выполнен",
    "calculation": "calculation расчет strength прочность fatigue усталость FEM нагрузки",
    "document": "drawing чертеж instruction инструкция disposition заключение report выпущен",
}

CATEGORY_MARKERS = {
    "problem": (
        "damage", "defect", "crack", "corrosion", "dent", "event", "zone", "aircraft",
        "поврежден", "дефект", "трещин", "корроз", "вмят", "зон", "самолет", "воздушн",
    ),
    "activity": (
        "assess", "develop", "inspect", "analysis", "repair", "performed", "completed", "defined",
        "оцен", "разработ", "инспек", "анализ", "ремонт", "выполн", "проведен", "определен",
    ),
    "calculation": (
        "strength", "fatigue", "damage tolerance", "fem", "load", "calculation", "substantiat", "stress",
        "прочност", "усталост", "живучест", "конечно-элемент", "нагруз", "расчет", "обоснован",
    ),
    "document": (
        "drawing", "instruction", "disposition", "report", "issued", "released", "deliverable",
        "чертеж", "инструкц", "заключен", "отчет", "выпущ", "разработана документац", "документ",
    ),
}

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
EVIDENCE_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
FENCED_JSON_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.IGNORECASE | re.DOTALL)
_DEFAULT_LLM = object()


class FactLLM(Protocol):
    def chat(self, system_prompt: str, user_prompt: str, allow_reasoning_fallback: bool = False) -> str: ...


def classify_reference_role(reference: dict[str, object], fact_category: str = "") -> str:
    probe = " ".join(
        str(reference.get(field) or "")
        for field in ("document_type", "document_number", "title", "raw_text")
    ).lower().replace("ё", "е")

    if any(term in probe for term in ("email", "e-mail", "cover letter", "meeting minutes", "transmittal", "письмо", "протокол совещ")):
        return "CONTEXT_ONLY"
    if re.search(r"\b(?:ap|ап)[-\s]?\d+\b", probe) or any(
        term in probe for term in ("internal procedure", "quality procedure", "процедур", "руководство по качеству")
    ):
        return "PROCESS_REFERENCE"
    if any(
        term in probe
        for term in (
            "repair drawing", "repair instruction", "technical disposition", "stress report",
            "compliance report", "inspection instruction", "ремонтный чертеж", "ремонтная инструкц",
            "техническое заключение", "отчет о прочности", "отчет о соответствии",
        )
    ):
        return "HISTORICAL_DELIVERABLE"
    if any(
        term in probe
        for term in (
            "loads report", "material allowable", "stress methodology", "fem report", "fatigue methodology",
            "damage tolerance methodology", "отчет по нагруз", "допускаемые напряж", "методик",
            "конечно-элемент", "усталост", "живучест",
        )
    ):
        return "ANALYSIS_BASIS"
    if re.search(r"\b(?:srm|amm|cmm|ndt|sb|ad)\b", probe) or any(
        term in probe for term in ("service bulletin", "airworthiness directive", "manual", "руководство по ремонту")
    ):
        return "TECHNICAL_BASIS"
    if fact_category == "calculation" and any(term in probe for term in ("load", "stress", "расчет", "прочност")):
        return "ANALYSIS_BASIS"
    return "UNKNOWN"


class CaseFactsService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        vector_index: MroQdrantIndex | None = None,
        llm: FactLLM | None | object = _DEFAULT_LLM,
    ) -> None:
        self.store = store
        self.vector_index = vector_index or MroQdrantIndex(store)
        if llm is _DEFAULT_LLM:
            settings = RuntimeSettings()
            self.llm = OpenAICompatibleLLM(settings) if settings.llm_enabled and settings.llm_provider == "openai" else None
        else:
            self.llm = llm if llm is None or hasattr(llm, "chat") else None

    def case_facts(self, request: CaseFactsRequest) -> CaseFactsResponse:
        resolution = self.store.resolve_case_id(request.case_id)
        if resolution.resolution_method == "UNRESOLVED" or not resolution.resolved_case_id:
            return self._response("CASE_NOT_FOUND", resolution)

        case_id = resolution.resolved_case_id
        corpus = self.store.fetch_case_corpus_summary(case_id)
        if not corpus.case_found:
            return self._response("CASE_NOT_FOUND", resolution, corpus)
        if corpus.document_count == 0:
            return self._response("CASE_FOUND_NO_DOCUMENTS", resolution, corpus)
        if corpus.chunk_count == 0:
            return self._response("CASE_FOUND_NO_CHUNKS", resolution, corpus)

        hits, retrieval_warnings, retrieval_available = self._retrieve_exact_case(
            case_id,
            request.categories,
            request.max_evidence_per_category,
        )
        if not retrieval_available:
            return self._response("RETRIEVAL_UNAVAILABLE", resolution, corpus, warnings=retrieval_warnings)

        warnings = list(retrieval_warnings)
        if self.llm is None:
            warnings.append("FACT_EXTRACTION_UNAVAILABLE")
            return self._response("FOUND", resolution, corpus, warnings=warnings)

        try:
            candidates = self._extract_candidates(case_id, request.categories, hits)
        except Exception:
            warnings.append("FACT_EXTRACTION_UNAVAILABLE")
            return self._response("FOUND", resolution, corpus, warnings=warnings)

        facts, validation_warnings = self._validate_facts(
            candidates,
            hits,
            request.categories,
            request.max_evidence_per_category,
            include_references=request.include_references,
        )
        warnings.extend(validation_warnings)
        listed_references = self._listed_only_references(case_id, facts) if request.include_references else []
        if not facts:
            warnings.append("NO_GROUNDED_FACTS")
        return self._response(
            "FOUND",
            resolution,
            corpus,
            facts=facts,
            listed_references=listed_references,
            warnings=warnings,
        )

    def _retrieve_exact_case(
        self,
        case_id: str,
        categories: list[str],
        max_per_category: int,
    ) -> tuple[list[dict[str, object]], list[str], bool]:
        query = " ".join(CATEGORY_QUERIES[category] for category in categories)
        vector_hits: list[dict[str, object]] = []
        vector_warnings: list[str] = []
        try:
            vector_hits, vector_warnings = self.vector_index.search(
                query,
                limit=max(24, max_per_category * len(categories) * 3),
                explicit_case_id=case_id,
            )
        except Exception:
            vector_warnings = ["vector search failed"]

        sqlite_hits: list[dict[str, object]] = []
        all_sqlite_hits: list[dict[str, object]] = []
        sqlite_available = True
        try:
            all_sqlite_hits = self.store.fetch_case_chunks(case_id, limit=10_000)
            sqlite_hits = self._rank_case_chunks(all_sqlite_hits, categories, max_per_category)
        except Exception:
            sqlite_available = False

        warnings: list[str] = []
        if any(warning == "CROSS_CASE_HIT_DROPPED" for warning in vector_warnings):
            warnings.append("CROSS_CASE_HIT_DROPPED")
        if vector_warnings and sqlite_available:
            warnings.append("QDRANT_UNAVAILABLE_SQLITE_FALLBACK")

        raw_hits = [*sqlite_hits, *vector_hits]
        authoritative: dict[str, dict[str, object]] = {}
        sqlite_by_id = {str(hit.get("chunk_id") or ""): hit for hit in all_sqlite_hits}
        for hit in raw_hits:
            if str(hit.get("case_id") or "") != case_id:
                if "CROSS_CASE_HIT_DROPPED" not in warnings:
                    warnings.append("CROSS_CASE_HIT_DROPPED")
                continue
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id or chunk_id in authoritative:
                continue
            chunk = sqlite_by_id.get(chunk_id)
            if chunk is None:
                continue
            if str(chunk.get("case_id") or "") != case_id:
                if "CROSS_CASE_HIT_DROPPED" not in warnings:
                    warnings.append("CROSS_CASE_HIT_DROPPED")
                continue
            if str(chunk.get("document_id") or "") != str(hit.get("document_id") or ""):
                continue
            authoritative[chunk_id] = chunk

        available = bool(authoritative) or sqlite_available
        if not available and not sqlite_available and not vector_hits:
            return [], warnings, False
        return list(authoritative.values()), warnings, available

    @staticmethod
    def _rank_case_chunks(
        chunks: list[dict[str, object]],
        categories: list[str],
        max_per_category: int,
    ) -> list[dict[str, object]]:
        selected: dict[str, dict[str, object]] = {}
        per_category_limit = max(8, max_per_category + 3)
        for category in categories:
            terms = [term.casefold() for term in WORD_RE.findall(CATEGORY_QUERIES[category])]
            ranked: list[tuple[int, int, dict[str, object]]] = []
            for chunk in chunks:
                text = str(chunk.get("text") or "")
                if not text.strip():
                    continue
                normalized_text = text.casefold().replace("ё", "е")
                section = str(chunk.get("section_title") or "").casefold().replace("ё", "е")
                score = sum(3 for term in terms if term in normalized_text)
                score += sum(2 for term in terms if term in section)
                if 60 <= len(text) <= 2400:
                    score += 3
                if str(chunk.get("chunk_kind") or "") == "table" or len(text) > 8000:
                    score -= 6
                if "ссылочн" in section or "reference document" in section:
                    score -= 5
                if score:
                    ranked.append((score, -abs(min(len(text), 6000) - 500), chunk))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            for _, _, chunk in ranked[:per_category_limit]:
                chunk_id = str(chunk.get("chunk_id") or "")
                if chunk_id:
                    selected.setdefault(chunk_id, chunk)
        if not selected:
            for chunk in chunks[: max(40, max_per_category * len(categories) * 5)]:
                chunk_id = str(chunk.get("chunk_id") or "")
                if chunk_id:
                    selected[chunk_id] = chunk
        return list(selected.values())

    def _extract_candidates(
        self,
        case_id: str,
        categories: list[str],
        hits: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        assert self.llm is not None
        system_prompt = (
            "You extract an evidence-backed historical work package from one completed MRO case. "
            "Allowed facts are the documented problem, engineering activities actually performed, "
            "calculations or analyses actually performed, and deliverable documents actually issued. "
            "Planned, proposed, required, or future work is not evidence of completed work. "
            "A filename, document title, or section heading alone is not a fact. "
            "Use only the supplied chunk IDs and document IDs. Never create IDs. "
            "Do not assess a new request, capability, approval, applicability, hours, labor, or price. "
            "Return JSON only: {\"facts\":[{\"category\":...,\"value\":...,\"document_id\":...,"
            "\"chunk_id\":...,\"evidence_text\":...}]}. evidence_text must be one continuous exact substring of chunk text."
        )
        chunk_payload = [
            {
                "case_id": case_id,
                "document_id": str(hit.get("document_id") or ""),
                "chunk_id": str(hit.get("chunk_id") or ""),
                "chunk_kind": str(hit.get("chunk_kind") or ""),
                "document_family": str(hit.get("document_family") or ""),
                "section_title": str(hit.get("section_title") or ""),
                "citation_refs": hit.get("citation_refs") if isinstance(hit.get("citation_refs"), list) else [],
                "text": str(hit.get("text") or "")[:1800],
            }
            for hit in hits[:32]
        ]
        content = self.llm.chat(
            system_prompt,
            json.dumps({"categories": categories, "chunks": chunk_payload}, ensure_ascii=False),
            allow_reasoning_fallback=False,
        ).strip()
        fenced = FENCED_JSON_RE.fullmatch(content)
        if fenced:
            content = fenced.group(1).strip()
        parsed = json.loads(content)
        raw_facts = parsed.get("facts") if isinstance(parsed, dict) else None
        if not isinstance(raw_facts, list):
            raise ValueError("fact extraction response does not contain a facts list")
        return [dict(item) for item in raw_facts if isinstance(item, dict)]

    def _validate_facts(
        self,
        candidates: list[dict[str, object]],
        hits: list[dict[str, object]],
        requested_categories: list[str],
        max_per_category: int,
        *,
        include_references: bool,
    ) -> tuple[list[HistoricalFact], list[str]]:
        hit_by_id = {str(hit.get("chunk_id") or ""): hit for hit in hits}
        facts: list[HistoricalFact] = []
        counts: defaultdict[str, int] = defaultdict(int)
        warnings: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            category = str(candidate.get("category") or "").strip().lower()
            value = str(candidate.get("value") or "").strip()
            document_id = str(candidate.get("document_id") or "").strip()
            chunk_id = str(candidate.get("chunk_id") or "").strip()
            evidence = str(candidate.get("evidence_text") or "").strip()
            hit = hit_by_id.get(chunk_id)
            valid = (
                category in requested_categories
                and counts[category] < max_per_category
                and bool(value and document_id and chunk_id and evidence)
                and hit is not None
                and str(hit.get("document_id") or "") == document_id
                and evidence in str(hit.get("text") or "")
                and self._substantive_evidence(category, value, evidence, hit or {})
            )
            key = (category, chunk_id, evidence)
            if not valid or key in seen:
                if "UNGROUNDED_FACT_DROPPED" not in warnings:
                    warnings.append("UNGROUNDED_FACT_DROPPED")
                continue
            references = []
            if include_references:
                evidence_ref_ids = {str(int(match.group(1))) for match in EVIDENCE_CITATION_RE.finditer(evidence)}
                references = [
                    self._reference_payload(reference, "DIRECTLY_CITED", category)
                    for reference in self._deduplicate_references(self.store.resolve_chunk_references(chunk_id))
                    if str(reference.get("ref_id") or "") in evidence_ref_ids
                ]
            facts.append(
                HistoricalFact(
                    category=category,
                    value=value,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    evidence_text=evidence,
                    references=references,
                )
            )
            seen.add(key)
            counts[category] += 1
        return facts, warnings

    @staticmethod
    def _substantive_evidence(category: str, value: str, evidence: str, hit: dict[str, object]) -> bool:
        normalized = " ".join(WORD_RE.findall(evidence.lower().replace("ё", "е")))
        if len(normalized) < 20 or len(WORD_RE.findall(normalized)) < 4:
            return False
        for title_field in ("section_title", "document_title"):
            title = " ".join(WORD_RE.findall(str(hit.get(title_field) or "").lower().replace("ё", "е")))
            if title and normalized == title:
                return False
        if not any(marker in normalized for marker in CATEGORY_MARKERS[category]):
            return False
        value_tokens = {token for token in WORD_RE.findall(value.lower().replace("ё", "е")) if len(token) >= 4}
        evidence_tokens = set(WORD_RE.findall(normalized))
        return not value_tokens or bool(value_tokens & evidence_tokens)

    def _listed_only_references(self, case_id: str, facts: list[HistoricalFact]) -> list[HistoricalReference]:
        directly_cited = {
            (reference.source_document_id, reference.ref_id, reference.source_table_id)
            for fact in facts
            for reference in fact.references
        }
        listed: list[HistoricalReference] = []
        for reference in self._deduplicate_references(self.store.fetch_case_references(case_id)):
            provenance = (
                str(reference.get("source_document_id") or ""),
                str(reference.get("ref_id") or ""),
                str(reference.get("source_table_id") or ""),
            )
            if provenance in directly_cited:
                continue
            listed.append(self._reference_payload(reference, "LISTED_ONLY"))
        return listed

    @staticmethod
    def _deduplicate_references(references: list[dict[str, object]]) -> list[dict[str, object]]:
        deduplicated: list[dict[str, object]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for reference in references:
            key = (
                str(reference.get("document_id") or ""),
                str(reference.get("ref_id") or ""),
                str(reference.get("source_document_id") or ""),
                str(reference.get("source_table_id") or ""),
            )
            if key not in seen:
                seen.add(key)
                deduplicated.append(reference)
        return deduplicated

    @staticmethod
    def _reference_payload(
        reference: dict[str, object],
        usage: str,
        fact_category: str = "",
    ) -> HistoricalReference:
        return HistoricalReference(
            ref_id=str(reference.get("ref_id") or ""),
            document_number=str(reference.get("document_number") or ""),
            title=str(reference.get("title") or ""),
            document_type=str(reference.get("document_type") or ""),
            raw_text=str(reference.get("raw_text") or ""),
            usage=usage,
            role=classify_reference_role(reference, fact_category),
            source_document_id=str(reference.get("source_document_id") or ""),
            source_table_id=str(reference.get("source_table_id") or ""),
        )

    @staticmethod
    def _response(
        status: str,
        resolution: CaseResolution,
        corpus: CorpusSummary | None = None,
        *,
        facts: list[HistoricalFact] | None = None,
        listed_references: list[HistoricalReference] | None = None,
        warnings: list[str] | None = None,
    ) -> CaseFactsResponse:
        return CaseFactsResponse(
            status=status,
            requested_case_id=resolution.requested_case_id,
            resolved_case_id=resolution.resolved_case_id,
            resolution_method=resolution.resolution_method,
            resolution_evidence=resolution.resolution_evidence,
            candidate_case_ids=resolution.candidate_case_ids,
            corpus=corpus or CorpusSummary(),
            facts=facts or [],
            listed_references=listed_references or [],
            warnings=list(dict.fromkeys(warnings or [])),
        )
