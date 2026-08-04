from __future__ import annotations

import re

from core.citations.formatting import obsidian_uri, source_label
from core.runtime_clients import ExternalReranker, OpenAICompatibleLLM, RuntimeSettings
from core.retrieval.vector import MroQdrantIndex, parent_id_for_chunk
from storage.sqlite.store import SQLiteStore


CASE_ID_RE = re.compile(r"\b(?:MRO|MP|WO|МР)-\d+\b", flags=re.IGNORECASE)
BARE_CASE_ID_RE = re.compile(r"\b(?:заявк[аеуи]?|wo|mro|mp|мр|номер|№)\s*[-№#:]?\s*(\d{2,5})\b", flags=re.IGNORECASE)
CITATION_REF_RE = re.compile(r"\[(\d{1,3})\]")
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9.-]{3,}")
RAW_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9.-]{1,}")
LIST_QUERY_RE = re.compile(r"\b(в\s+каких\s+заявках|в\s+какой\s+заявке|где\s+упоминается|где\s+есть)\b", flags=re.IGNORECASE)
STOP_TOKENS = {
    "заявках",
    "заявке",
    "какой",
    "каких",
    "где",
    "есть",
    "была",
    "были",
    "ли",
    "что",
    "про",
    "это",
    "для",
    "как",
    "with",
    "from",
    "repair",
}
def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class RetrievalService:
    def __init__(self, store: SQLiteStore, vault_name: str = "mro_markdown") -> None:
        self.store = store
        self.vault_name = vault_name
        self.settings = RuntimeSettings()
        self._reranker = ExternalReranker(self.settings.reranker_url, batch_size=self.settings.reranker_batch_size) if (
            self.settings.reranker_enabled and self.settings.reranker_url
        ) else None
        self._llm = OpenAICompatibleLLM(self.settings) if self.settings.llm_enabled and self.settings.llm_provider == "openai" else None
        self._vector_index = MroQdrantIndex(store)
        self._last_vector_warnings: list[str] = []

    def chat(self, question: str, limit: int = 6) -> dict[str, object]:
        explicit_case_id = self._extract_case_id(question)
        case_payload = self.store.fetch_case(explicit_case_id) if explicit_case_id else None
        requested_references = self._requested_reference_records(question, case_payload)
        hits = self._collect_candidates(question, limit=max(limit * 6, 36), explicit_case_id=explicit_case_id)
        if not self._is_list_query(question) and not self._is_case_overview_query(question):
            hits = self._rerank_hits(question, hits, limit=max(limit * 6, 36))
        hits = self._select_hits(question, hits, limit=limit)
        sources: list[dict[str, object]] = []
        for hit in hits:
            note_path = str(hit.get("vault_note_path") or "")
            block_id = str(hit.get("block_id") or "")
            source_document_id = str(hit.get("source_document_id") or hit["document_id"])
            source_text = self._source_text_for_hit(hit, question)
            snippet_limit = 5000 if len(source_text) > 1600 else 1600
            citation_refs = [str(item) for item in hit.get("citation_refs", [])] if isinstance(hit.get("citation_refs"), list) else []
            resolved_references = self._resolved_references_for_hit(hit)
            sources.append(
                {
                    "title": source_label(self._display_case_id(str(hit["case_id"])), source_document_id, str(hit["section_title"] or "-")),
                    "snippet": source_text[:snippet_limit],
                    "source_type": "document_chunk",
                    "source_descriptor": {
                        "case_id": self._display_case_id(str(hit["case_id"])),
                        "internal_case_id": hit["case_id"],
                        "document_id": hit["document_id"],
                        "source_document_id": source_document_id,
                        "chunk_id": hit["chunk_id"],
                        "parent_id": str(hit.get("parent_id") or parent_id_for_chunk(hit)),
                        "parent_kind": str(hit.get("parent_kind") or ("section" if str(hit.get("section_title") or "") else "document")),
                        "child_count": int(hit.get("child_count") or 1),
                        "chunk_kind": str(hit.get("chunk_kind") or "chunk"),
                        "section_title": hit["section_title"],
                        "page_number": None,
                        "vault_uri": obsidian_uri(self.vault_name, note_path, block_id) if note_path else "",
                        "page_image_uri": str(hit.get("page_image_path") or ""),
                        "citation_refs": citation_refs,
                        "resolved_references": resolved_references,
                        "snapshot_id": None,
                        "retrieval_mode": str(hit.get("retrieval_mode") or ("qdrant_hybrid" if hit.get("vector_score") is not None else "sqlite_like")),
                        "link_confidence": 1.0,
                        "rerank_score": float(hit.get("rerank_score", 0.0)),
                        "final_score": float(hit.get("final_score", 0.0)),
                    },
                }
            )
        answer = self._build_answer(question, case_payload, sources, requested_references)
        llm_status = "retrieval_only"
        if self._llm is not None and sources and not self._is_list_query(question):
            try:
                llm_answer = self._generate_llm_answer(question, case_payload, sources, requested_references)
                if llm_answer.strip():
                    answer = llm_answer
                    llm_status = "openai_compatible"
                else:
                    llm_status = "retrieval_fallback"
            except Exception:
                llm_status = "retrieval_fallback"
        warnings = list(self._last_vector_warnings)
        if not sources:
            warnings.append("no_relevant_sources_found")
        return {
            "answer": answer,
            "sources": sources,
            "evidence": sources[: min(3, len(sources))],
            "warnings": warnings,
            "snapshot_id": None,
            "llm_status": llm_status,
        }

    def health(self) -> dict[str, object]:
        return {
            "mro_kb_vectors": self._vector_index.health(),
            "reranker": self._reranker.health() if self._reranker is not None else {"ok": False, "disabled": True},
            "llm": self._llm.health() if self._llm is not None else {"ok": False, "disabled": True},
        }

    def reindex_vectors(self, limit: int = 0) -> dict[str, object]:
        return self._vector_index.reindex(limit=limit)

    def vector_status(self) -> dict[str, object]:
        return {
            "progress": self._vector_index.reindex_status(),
            "health": self._vector_index.health(),
        }

    @staticmethod
    def _extract_case_id(question: str) -> str | None:
        match = CASE_ID_RE.search(question or "")
        if match:
            return RetrievalService._normalize_case_id(match.group(0))
        bare_match = BARE_CASE_ID_RE.search(question or "")
        if bare_match:
            return f"MRO-{int(bare_match.group(1)):03d}"
        return None

    @staticmethod
    def _normalize_case_id(value: str) -> str:
        digits = re.findall(r"\d+", value or "")
        if not digits:
            return value.upper()
        return f"MRO-{int(digits[0]):03d}"

    def _requested_reference_records(self, question: str, case_payload: dict[str, object] | None) -> list[dict[str, object]]:
        if not case_payload:
            return []
        ref_ids = [str(int(match.group(1))) for match in CITATION_REF_RE.finditer(question or "")]
        if not ref_ids:
            return []
        return self.store.resolve_case_references(str(case_payload.get("case_id") or ""), ref_ids)

    @staticmethod
    def _display_case_id(value: str) -> str:
        digits = re.findall(r"\d+", value or "")
        if not digits:
            return value
        return f"MRO-{int(digits[0]):03d}"

    def _build_answer(
        self,
        question: str,
        case_payload: dict[str, object] | None,
        sources: list[dict[str, object]],
        requested_references: list[dict[str, object]] | None = None,
    ) -> str:
        lines: list[str] = []
        if case_payload:
            case_id = self._display_case_id(str(case_payload["case_id"]))
            aircraft = str(case_payload.get("aircraft_type") or "-")
            msn = str(case_payload.get("msn") or "-")
            if self._is_case_overview_query(question):
                summary = self._case_overview_summary(case_payload)
                if summary:
                    lines.append(f"{case_id}: {summary}")
                else:
                    lines.append(f"{case_id}: в найденных данных есть карточка заявки для {aircraft}, MSN {msn}.")
                if case_payload.get("ata_list"):
                    lines.append(f"ATA: {', '.join(str(item) for item in case_payload['ata_list'])}.")
                if case_payload.get("applicable_ap_refs"):
                    ap_refs = [str(item) for item in case_payload["applicable_ap_refs"]]
                    lines.append(f"Применимые AP ({len(ap_refs)} пунктов): {', '.join(ap_refs)}.")
            elif self._is_evidence_documents_query(question):
                evidence_docs = self._case_evidence_documents(str(case_payload["case_id"]))
                if evidence_docs:
                    lines.append(f"По {case_id} в перечне доказательных документов указаны:")
                    lines.extend(f"- {item}" for item in evidence_docs[:12])
                else:
                    lines.append(f"По {case_id} я не нашел надежно разобранный перечень доказательных документов.")
            else:
                lines.append(f"По {case_id} ({aircraft}, MSN {msn}) нашел релевантные фрагменты в базе MRO KB.")
                if case_payload.get("subject"):
                    lines.append(f"Тема заявки: {case_payload['subject']}")
        if requested_references:
            if lines:
                lines.append("")
            lines.append("Расшифровка запрошенных ссылок:")
            lines.extend(self._reference_lines(requested_references, limit=12, include_source=True))
        if not lines and sources:
            lines.append("По найденным фрагментам MRO KB:")
            for item in sources[:3]:
                lines.append(f"- {normalize_spaces(str(item.get('snippet') or ''))[:260]}")
        if not lines:
            lines.append("В найденных документах надежного ответа нет.")
            return "\n".join(lines)
        if sources:
            if case_payload and self._is_evidence_documents_query(question):
                evidence_source = self._case_evidence_source(str(case_payload["case_id"]))
                if evidence_source:
                    sources = [evidence_source, *sources]
            display_sources = self._ordered_sources_for_answer(question, sources)
            case_ids = self._case_ids_from_sources(display_sources)
            if self._is_list_query(question):
                lines.append("")
                lines.append("Подтвержденные заявки:")
                for case_id in case_ids:
                    lines.append(f"- {case_id}")
            if case_payload and self._is_case_overview_query(question):
                evidence = self._case_overview_evidence(display_sources)
                if evidence:
                    lines.append("")
                    lines.append("Подтверждающие документы:")
                    lines.extend(evidence)
            lines.append("")
            lines.append(self._source_summary(display_sources))
            reference_summary = self._reference_summary(display_sources)
            if reference_summary:
                lines.append("")
                lines.append(reference_summary)
            lines.append("")
            lines.append(self._sources_table(display_sources))
        return "\n".join(lines)

    @staticmethod
    def _case_overview_summary(case_payload: dict[str, object]) -> str:
        candidates = [
            str(case_payload.get("problem_summary") or "").strip(),
            str(case_payload.get("subject") or "").strip(),
        ]
        documents = case_payload.get("documents") if isinstance(case_payload.get("documents"), list) else []
        for document in documents:
            if not isinstance(document, dict):
                continue
            family = str(document.get("document_family") or "")
            if family in {"technical_disposition", "equivalent_justification", "opi"}:
                candidates.append(str(document.get("subject") or "").strip())
                candidates.append(str(document.get("title") or "").strip())
        candidates = [normalize_spaces(item) for item in candidates if normalize_spaces(item)]
        if not candidates:
            return ""
        complete = [item for item in candidates if not item.endswith(":")]
        if complete:
            return max(complete, key=len)
        return max(candidates, key=len)

    @staticmethod
    def _is_case_overview_query(question: str) -> bool:
        normalized = (question or "").lower().replace("ё", "е")
        return bool(
            re.search(r"\b(о\s+чем|про\s+что|что\s+за|кратк[оая]+|суть|описани[ея])\b", normalized)
            and re.search(r"\b(заявк|mro|mp|wo|мр|номер|№|\d{2,5})", normalized)
        )

    @staticmethod
    def _is_evidence_documents_query(question: str) -> bool:
        normalized = (question or "").lower().replace("ё", "е")
        return bool(
            re.search(r"\b(доказательн|дд|подтверждающ)", normalized)
            and re.search(r"\b(документ|переч|спис|какие|какой)", normalized)
        )

    def _case_evidence_documents(self, case_id: str) -> list[str]:
        rows: list[str] = []
        seen: set[str] = set()
        for hit in self._case_evidence_hits(case_id):
            section = str(hit.get("section_title") or "").lower().replace("ё", "е")
            text = str(hit.get("text") or "")
            if "доказательн" not in section and "номер дд" not in text.lower().replace("ё", "е"):
                continue
            for row in self._parse_evidence_document_rows(text):
                if row in seen:
                    continue
                seen.add(row)
                rows.append(row)
                if len(rows) >= 20:
                    return rows
        return rows

    def _case_evidence_hits(self, case_id: str) -> list[dict[str, object]]:
        return self.store.search_text("перечень доказательных документов номер ДД название ДД", limit=12, case_ids=[case_id])

    def _case_evidence_source(self, case_id: str) -> dict[str, object] | None:
        for hit in self._case_evidence_hits(case_id):
            section = str(hit.get("section_title") or "")
            text = str(hit.get("text") or "")
            normalized = f"{section}\n{text}".lower().replace("ё", "е")
            if "доказательн" not in normalized and "номер дд" not in normalized:
                continue
            note_path = str(hit.get("vault_note_path") or "")
            block_id = str(hit.get("block_id") or "")
            source_document_id = str(hit.get("source_document_id") or hit.get("document_id") or "")
            return {
                "title": source_label(self._display_case_id(str(hit.get("case_id") or "")), source_document_id, section or "-"),
                "snippet": text[:1600],
                "source_type": "document_chunk",
                "source_descriptor": {
                    "case_id": self._display_case_id(str(hit.get("case_id") or "")),
                    "internal_case_id": hit.get("case_id"),
                    "document_id": hit.get("document_id"),
                    "source_document_id": source_document_id,
                    "chunk_id": hit.get("chunk_id"),
                    "parent_id": str(hit.get("parent_id") or parent_id_for_chunk(hit)),
                    "parent_kind": str(hit.get("parent_kind") or ("section" if section else "document")),
                    "child_count": int(hit.get("child_count") or 1),
                    "chunk_kind": str(hit.get("chunk_kind") or "chunk"),
                    "section_title": section,
                    "page_number": None,
                    "vault_uri": obsidian_uri(self.vault_name, note_path, block_id) if note_path else "",
                    "page_image_uri": str(hit.get("page_image_path") or ""),
                    "citation_refs": hit.get("citation_refs") if isinstance(hit.get("citation_refs"), list) else [],
                    "resolved_references": self._resolved_references_for_hit(hit),
                    "snapshot_id": None,
                    "retrieval_mode": str(hit.get("retrieval_mode") or "sqlite_like"),
                    "link_confidence": 1.0,
                    "rerank_score": float(hit.get("rerank_score", 0.0)),
                    "final_score": float(hit.get("final_score", 0.0)),
                },
            }
        return None

    @staticmethod
    def _parse_evidence_document_rows(text: str) -> list[str]:
        rows: list[str] = []
        for line in (text or "").splitlines():
            if "|" not in line:
                continue
            cells = [normalize_spaces(cell.strip(" *")) for cell in line.strip().strip("|").split("|")]
            cells = [cell for cell in cells if cell]
            if len(cells) < 2:
                continue
            joined = " ".join(cells).lower().replace("ё", "е")
            if re.fullmatch(r"[-: ]+", "".join(cells)):
                continue
            if "номер дд" in joined or "название дд" in joined or "категория" in joined:
                continue
            meaningful = [cell for cell in cells if not re.fullmatch(r"\d+", cell)]
            if len(meaningful) < 2:
                continue
            doc_number = meaningful[0]
            title = meaningful[1]
            category = next((cell for cell in reversed(cells) if re.fullmatch(r"\d+", cell)), "")
            rendered = f"{doc_number} — {title}"
            if category:
                rendered += f" (категория {category})"
            rows.append(rendered)
        return rows

    @staticmethod
    def _case_overview_evidence(sources: list[dict[str, object]]) -> list[str]:
        rows: list[str] = []
        seen: set[tuple[str, str]] = set()
        for source in sources:
            descriptor = source.get("source_descriptor") or {}
            document_id = str(descriptor.get("source_document_id") or "").strip()
            section = str(descriptor.get("section_title") or "").strip()
            if not document_id:
                continue
            key = (document_id, section)
            if key in seen:
                continue
            seen.add(key)
            rows.append(f"- {document_id}" + (f", раздел {section}" if section else ""))
            if len(rows) >= 4:
                break
        return rows

    def _ordered_sources_for_answer(self, question: str, sources: list[dict[str, object]]) -> list[dict[str, object]]:
        if not self._is_evidence_documents_query(question):
            return sources

        def priority(source: dict[str, object]) -> tuple[int, str]:
            descriptor = source.get("source_descriptor") or {}
            section = str(descriptor.get("section_title") or "").lower().replace("ё", "е")
            snippet = str(source.get("snippet") or "").lower().replace("ё", "е")
            if "доказательн" in section:
                return (0, section)
            if "номер дд" in snippet or "название дд" in snippet:
                return (1, section)
            return (2, section)

        return sorted(sources, key=priority)

    def _rerank_hits(self, question: str, hits: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        if not hits:
            return []
        if self._reranker is None:
            return hits[:limit]
        pairs = []
        for hit in hits:
            pairs.append(
                [
                    question,
                    "\n".join(
                        part
                        for part in [
                            str(hit.get("subject") or ""),
                            str(hit.get("problem_summary") or ""),
                            str(hit.get("document_family") or ""),
                            str(hit.get("section_title") or ""),
                            str(hit.get("heading_path") or ""),
                            str(hit.get("search_text") or ""),
                            str(hit.get("text") or ""),
                        ]
                        if part
                    ),
                ]
            )
        try:
            scores = self._reranker.compute_score(pairs, normalize=True)
        except Exception:
            return hits[:limit]
        rescored = []
        for hit, score in zip(hits, scores, strict=False):
            updated = dict(hit)
            updated["rerank_score"] = float(score)
            rescored.append(updated)
        rescored.sort(key=lambda item: float(item.get("rerank_score", 0.0)), reverse=True)
        return rescored[:limit]

    def _collect_candidates(self, question: str, limit: int, explicit_case_id: str | None = None) -> list[dict[str, object]]:
        self._last_vector_warnings = []
        lexical_hits = self._collect_sqlite_candidates(question, limit=limit, explicit_case_id=explicit_case_id)
        if self._is_list_query(question):
            return lexical_hits
        if explicit_case_id and (self._is_case_overview_query(question) or CITATION_REF_RE.search(question or "")):
            return lexical_hits
        vector_hits, warnings = self._vector_index.search(question, limit=limit, explicit_case_id=explicit_case_id)
        self._last_vector_warnings = warnings
        if not vector_hits:
            return lexical_hits
        merged = self._vector_index.hybrid_merge(question, vector_hits, lexical_hits, limit=max(limit, 24))
        return merged or lexical_hits

    def _collect_sqlite_candidates(self, question: str, limit: int, explicit_case_id: str | None = None) -> list[dict[str, object]]:
        per_query_limit = min(max(limit // 4, 8), 12)
        merged: dict[str, dict[str, object]] = {}
        case_ids: list[str] = []
        seen_cases: set[str] = set()
        if explicit_case_id:
            seen_cases.add(explicit_case_id)
            case_ids.append(explicit_case_id)
        candidate_queries = self._candidate_queries(question)
        for query in ([] if explicit_case_id else candidate_queries[:3]):
            for doc in self.store.search_documents(query, limit=12):
                case_id = str(doc["case_id"])
                if case_id not in seen_cases:
                    seen_cases.add(case_id)
                    case_ids.append(case_id)
            for row in self.store.search_cases(query, limit=4):
                case_id = str(row["case_id"])
                if case_id not in seen_cases:
                    seen_cases.add(case_id)
                    case_ids.append(case_id)
        scoped_queries = candidate_queries[:8] if explicit_case_id else candidate_queries[:3]
        for query in scoped_queries:
            if explicit_case_id:
                for hit in self.store.search_text(query, limit=per_query_limit, case_ids=[explicit_case_id]):
                    chunk_id = str(hit.get("chunk_id") or "")
                    existing = merged.get(chunk_id)
                    if existing is None or float(hit.get("lexical_score", 0.0)) > float(existing.get("lexical_score", 0.0)):
                        merged[chunk_id] = hit
                continue
            for doc in self.store.search_documents_for_cases(query, case_ids[:18], limit=18 if case_ids else 8):
                doc_score = float(doc.get("lexical_score", 0.0))
                for chunk in self.store.fetch_document_chunks(str(doc["document_id"]), limit=12):
                    chunk_id = str(chunk.get("chunk_id") or "")
                    enriched = dict(chunk)
                    enriched["lexical_score"] = max(float(enriched.get("lexical_score", 0.0)), doc_score)
                    existing = merged.get(chunk_id)
                    if existing is None or float(enriched.get("lexical_score", 0.0)) > float(existing.get("lexical_score", 0.0)):
                        merged[chunk_id] = enriched
            if not explicit_case_id and len(merged) < 6 and query in candidate_queries[:1]:
                for hit in self.store.search_text(query, limit=per_query_limit):
                    chunk_id = str(hit.get("chunk_id") or "")
                    existing = merged.get(chunk_id)
                    if existing is None or float(hit.get("lexical_score", 0.0)) > float(existing.get("lexical_score", 0.0)):
                        merged[chunk_id] = hit
        if explicit_case_id and not merged:
            case_payload = self.store.fetch_case(explicit_case_id)
            if case_payload:
                for document in case_payload.get("documents", []):
                    document_id = str(document.get("document_id") or "")
                    for chunk in self.store.fetch_document_chunks(document_id, limit=10):
                        chunk_id = str(chunk.get("chunk_id") or "")
                        if chunk_id and chunk_id not in merged:
                            merged[chunk_id] = chunk
        hits = list(merged.values())
        for hit in hits:
            hit.setdefault("parent_id", parent_id_for_chunk(hit))
            hit.setdefault("parent_kind", "section" if str(hit.get("section_title") or "") else "document")
        hits.sort(key=lambda item: float(item.get("lexical_score", 0.0)), reverse=True)
        selected: list[dict[str, object]] = []
        per_case_counts: dict[str, int] = {}
        target = max(limit, 24)
        max_per_case = target if explicit_case_id else 6
        for hit in hits:
            case_id = str(hit.get("case_id") or "")
            if per_case_counts.get(case_id, 0) >= max_per_case:
                continue
            selected.append(hit)
            per_case_counts[case_id] = per_case_counts.get(case_id, 0) + 1
            if len(selected) >= target:
                break
        if len(selected) < min(target, len(hits)):
            seen_ids = {str(hit.get("chunk_id") or "") for hit in selected}
            for hit in hits:
                chunk_id = str(hit.get("chunk_id") or "")
                if chunk_id in seen_ids:
                    continue
                selected.append(hit)
                seen_ids.add(chunk_id)
                if len(selected) >= target:
                    break
        return selected

    def _select_hits(self, question: str, hits: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        tokens = self._query_tokens(question)
        rescored: list[dict[str, object]] = []
        for hit in hits:
            if self._is_list_query(question) and not self._list_hit_matches_question(tokens, hit):
                continue
            score = float(hit.get("rerank_score", hit.get("hybrid_score", hit.get("lexical_score", 0.0))))
            score += self._match_bonus(tokens, hit)
            score -= self._noise_penalty(hit)
            updated = dict(hit)
            updated["final_score"] = score
            rescored.append(updated)
        rescored.sort(key=lambda item: float(item.get("final_score", 0.0)), reverse=True)

        selected: list[dict[str, object]] = []
        seen_chunk_ids: set[str] = set()
        seen_case_ids: set[str] = set()
        prefer_case_diversity = self._is_list_query(question)
        for hit in rescored:
            chunk_id = str(hit.get("chunk_id") or "")
            if chunk_id in seen_chunk_ids:
                continue
            if self._is_low_value_hit(hit):
                continue
            case_id = str(hit.get("case_id") or "")
            if prefer_case_diversity and case_id in seen_case_ids and len(selected) < max(limit, 4):
                continue
            seen_chunk_ids.add(chunk_id)
            seen_case_ids.add(case_id)
            selected.append(hit)
            if len(selected) >= limit:
                break

        if not selected:
            return rescored[:limit]
        return selected

    @staticmethod
    def _list_hit_matches_question(tokens: list[str], hit: dict[str, object]) -> bool:
        haystack = "\n".join(
            str(hit.get(key) or "")
            for key in (
                "section_title",
                "section_label",
                "title",
                "document_family",
                "subject",
                "problem_summary",
                "search_text",
                "text",
            )
        ).lower().replace("ё", "е")
        for token in (token for token in tokens if token.isdigit()):
            if token not in haystack:
                return False
        content_tokens = [token for token in tokens if not token.isdigit()]
        if not content_tokens:
            return True
        matched = sum(1 for token in content_tokens if RetrievalService._token_in_text(token, haystack))
        return matched >= min(2, len(content_tokens))

    @staticmethod
    def _query_tokens(question: str) -> list[str]:
        normalized = (question or "").lower().replace("ё", "е")
        tokens = []
        for token in RAW_TOKEN_RE.findall(normalized):
            if token in STOP_TOKENS:
                continue
            if len(token) < 3 and not token.isdigit():
                continue
            tokens.append(token)
        return tokens

    @staticmethod
    def _raw_query_tokens(question: str) -> list[str]:
        normalized = (question or "").lower().replace("ё", "е")
        return RAW_TOKEN_RE.findall(normalized)

    def _candidate_queries(self, question: str) -> list[str]:
        tokens = self._query_tokens(question)
        raw_tokens = self._raw_query_tokens(question)
        ordered: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            cleaned = normalize_spaces(value)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered.append(cleaned)

        add(question)
        if tokens:
            add(" ".join(tokens[:4]))
        ranked_tokens = sorted(tokens, key=lambda token: (-len(token), token))
        for token in ranked_tokens[:6]:
            add(token)
        for idx in range(len(raw_tokens) - 1):
            left = raw_tokens[idx]
            right = raw_tokens[idx + 1]
            if left in STOP_TOKENS or right in STOP_TOKENS:
                continue
            if len(left) < 2 and not left.isdigit():
                continue
            if len(right) < 2 and not right.isdigit():
                continue
            pair = f"{left} {right}"
            add(pair)
            if len(ordered) >= 8:
                break
        return ordered[:8]

    def _source_text_for_hit(self, hit: dict[str, object], question: str) -> str:
        text = normalize_spaces(str(hit.get("text") or ""))
        section_title = str(hit.get("section_title") or "")
        document_id = str(hit.get("document_id") or "")
        if not section_title or not document_id:
            return self._focused_text(text, question)
        section_chunks = self.store.fetch_section_chunks(document_id, section_title, limit=80)
        section_texts: list[str] = []
        seen: set[str] = set()
        for chunk in section_chunks:
            chunk_text = normalize_spaces(str(chunk.get("text") or ""))
            if not chunk_text or chunk_text in seen:
                continue
            seen.add(chunk_text)
            section_texts.append(chunk_text)
        expanded = "\n".join(section_texts) or text
        return self._focused_text(expanded, question)

    def _resolved_references_for_hit(self, hit: dict[str, object]) -> list[dict[str, object]]:
        chunk_ids: list[str] = []
        child_chunk_ids = hit.get("child_chunk_ids")
        if isinstance(child_chunk_ids, list):
            chunk_ids.extend(str(item) for item in child_chunk_ids if str(item or "").strip())
        chunk_id = str(hit.get("chunk_id") or "").strip()
        if chunk_id:
            chunk_ids.append(chunk_id)
        references: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for candidate_chunk_id in chunk_ids[:80]:
            for reference in self.store.resolve_chunk_references(candidate_chunk_id):
                key = (str(reference.get("document_id") or ""), str(reference.get("ref_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                references.append(reference)
        return references

    @staticmethod
    def _focused_text(text: str, question: str, window: int = 4200) -> str:
        if len(text) <= window:
            return text
        lowered = text.lower()
        positions: list[int] = []
        for token in RetrievalService._query_tokens(question):
            if token.isdigit():
                continue
            position = lowered.find(token)
            if position < 0 and token.isalpha() and len(token) >= 6:
                position = lowered.find(token[:6])
            if position >= 0:
                positions.append(position)
        if not positions:
            return text[:window]
        center = min(positions)
        start = max(center - window // 3, 0)
        end = min(start + window, len(text))
        return text[start:end]

    @staticmethod
    def _token_in_text(token: str, text: str) -> bool:
        if token in text:
            return True
        if token.isalpha() and len(token) >= 6:
            return token[:6] in text
        return False

    @staticmethod
    def _case_ids_from_sources(sources: list[dict[str, object]]) -> list[str]:
        case_ids: list[str] = []
        seen: set[str] = set()
        for source in sources:
            descriptor = source.get("source_descriptor") or {}
            case_id = str(descriptor.get("case_id") or "").strip()
            if case_id and case_id not in seen:
                seen.add(case_id)
                case_ids.append(case_id)
        return case_ids

    @staticmethod
    def _is_list_query(question: str) -> bool:
        return bool(LIST_QUERY_RE.search(question or ""))

    def _match_bonus(self, tokens: list[str], hit: dict[str, object]) -> float:
        haystack = "\n".join(
            [
                str(hit.get("section_title") or "").lower(),
                str(hit.get("section_label") or "").lower(),
                str(hit.get("heading_path") or "").lower(),
                str(hit.get("title") or "").lower(),
                str(hit.get("document_family") or "").lower(),
                str(hit.get("subject") or "").lower(),
                str(hit.get("problem_summary") or "").lower(),
                str(hit.get("search_text") or "").lower(),
                str(hit.get("text") or "").lower(),
            ]
        ).replace("ё", "е")
        bonus = 0.0
        matched = 0
        for token in tokens:
            if token in haystack:
                matched += 1
                bonus += 0.14
        section_title = str(hit.get("section_title") or "").lower()
        if any(token in section_title for token in tokens):
            bonus += 0.18
        bonus += self._phrase_bonus(tokens, haystack)
        if matched >= 2:
            bonus += 0.15
        return bonus

    @staticmethod
    def _phrase_bonus(tokens: list[str], haystack: str) -> float:
        if len(tokens) < 2:
            return 0.0
        bonus = 0.0
        seen: set[str] = set()
        for size, weight in ((3, 0.26), (2, 0.16)):
            if len(tokens) < size:
                continue
            for start in range(0, len(tokens) - size + 1):
                phrase = " ".join(tokens[start : start + size])
                if phrase in seen:
                    continue
                seen.add(phrase)
                if phrase in haystack:
                    bonus += weight
        return bonus

    @staticmethod
    def _noise_penalty(hit: dict[str, object]) -> float:
        penalty = 0.0
        text = normalize_spaces(str(hit.get("text") or ""))
        section_title = str(hit.get("section_title") or "")
        chunk_kind = str(hit.get("chunk_kind") or "")
        if "<!-- image -->" in text and len(text) < 600:
            penalty += 0.8
        if len(text) < 80:
            penalty += 0.2
        if chunk_kind == "table" and len(text) < 180:
            penalty += 0.1
        return penalty

    @staticmethod
    def _is_low_value_hit(hit: dict[str, object]) -> bool:
        text = normalize_spaces(str(hit.get("text") or ""))
        if not text:
            return True
        if "<!-- image -->" in text and len(text) < 600:
            return True
        return False

    def _generate_llm_answer(
        self,
        question: str,
        case_payload: dict[str, object] | None,
        sources: list[dict[str, object]],
        requested_references: list[dict[str, object]] | None = None,
    ) -> str:
        assert self._llm is not None
        system_prompt = (
            "Ты инженерный помощник по MRO knowledge base. "
            "Отвечай только по переданным фрагментам. "
            "Не выдумывай факты и номера заявок. "
            "Если подтверждений недостаточно, так и напиши. "
            "Отвечай кратко, на языке пользователя, с опорой на источники."
        )
        lines = [f"Вопрос:\n{question.strip()}\n"]
        if case_payload:
            lines.append(
                "Контекст заявки:\n"
                f"case_id={self._display_case_id(str(case_payload.get('case_id') or ''))}\n"
                f"aircraft_type={case_payload.get('aircraft_type')}\n"
                f"msn={case_payload.get('msn')}\n"
                f"subject={case_payload.get('subject')}\n"
            )
        if requested_references:
            lines.append("Расшифровка запрошенных ссылок:")
            lines.extend(self._reference_lines(requested_references, limit=12, include_source=True))
        lines.append("Найденные фрагменты:")
        for idx, source in enumerate(sources[:4], start=1):
            descriptor = source.get("source_descriptor") or {}
            resolved_refs = descriptor.get("resolved_references") if isinstance(descriptor, dict) else []
            reference_lines = []
            if isinstance(resolved_refs, list):
                for reference in resolved_refs[:6]:
                    if not isinstance(reference, dict):
                        continue
                    reference_lines.append(
                        f"{reference.get('marker') or '[' + str(reference.get('ref_id') or '') + ']'} "
                        f"{reference.get('document_number') or ''} {reference.get('title') or reference.get('raw_text') or ''}".strip()
                    )
            lines.append(
                f"[{idx}]\n"
                f"case_id={descriptor.get('case_id', '')}\n"
                f"document={descriptor.get('source_document_id', '')}\n"
                f"section={descriptor.get('section_title', '')}\n"
                f"chunk_id={descriptor.get('chunk_id', '')}\n"
                f"resolved_references={'; '.join(reference_lines)}\n"
                f"snippet={str(source['snippet'])[:1200]}\n"
            )
        user_prompt = "\n".join(lines)
        answer = self._llm.chat(system_prompt, user_prompt).strip()
        if not answer:
            return answer
        suffix = self._source_summary(sources)
        appendix = f"{suffix}\n\n{self._sources_table(sources)}" if suffix else self._sources_table(sources)
        return f"{answer}\n\n{appendix}"

    def _sources_table(self, sources: list[dict[str, object]]) -> str:
        rows = [
            "### Источники",
            "",
            "| Документ | Раздел | Текст чанка | Ссылка |",
            "|---|---|---|---|",
        ]
        seen: set[tuple[str, str, str]] = set()
        for source in sources[:6]:
            descriptor = source.get("source_descriptor") or {}
            link_target = str(descriptor.get("vault_uri") or "").strip()
            link = f"[Открыть]({link_target})" if link_target else ""
            chunk_text = normalize_spaces(str(source.get("snippet") or ""))
            if len(chunk_text) > 220:
                chunk_text = f"{chunk_text[:217]}..."
            chunk_text = chunk_text.replace("|", "\\|")
            key = (
                str(descriptor.get("source_document_id", "")),
                str(descriptor.get("section_title", "")),
                chunk_text[:120],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                f"| {descriptor.get('source_document_id', '')} | "
                f"{descriptor.get('section_title', '')} | "
                f"{chunk_text} | "
                f"{link} |"
            )
        return "\n".join(rows)

    def _source_summary(self, sources: list[dict[str, object]]) -> str:
        if not sources:
            return ""
        descriptor = sources[0].get("source_descriptor") or {}
        parts = []
        document_id = str(descriptor.get("source_document_id") or "").strip()
        section = str(descriptor.get("section_title") or "").strip()
        case_id = str(descriptor.get("case_id") or "").strip()
        if case_id:
            parts.append(case_id)
        if document_id:
            parts.append(document_id)
        if section:
            parts.append(f"раздел {section}")
        if not parts:
            return ""
        summary = ", ".join(parts)
        vault_uri = str(descriptor.get("vault_uri") or "").strip()
        if vault_uri:
            return f"Источник: [{summary}]({vault_uri})."
        return f"Источник: {summary}."

    @staticmethod
    def _reference_summary(sources: list[dict[str, object]]) -> str:
        rows: list[str] = []
        seen: set[tuple[str, str]] = set()
        for source in sources:
            descriptor = source.get("source_descriptor") or {}
            resolved_refs = descriptor.get("resolved_references") if isinstance(descriptor, dict) else []
            if not isinstance(resolved_refs, list):
                continue
            for reference in resolved_refs:
                if not isinstance(reference, dict):
                    continue
                key = (str(reference.get("document_id") or ""), str(reference.get("ref_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                rendered = RetrievalService._reference_line(reference, include_source=False)
                if rendered:
                    rows.append(rendered)
                if len(rows) >= 8:
                    break
            if len(rows) >= 8:
                break
        if not rows:
            return ""
        return "Расшифровка ссылок из найденных фрагментов:\n" + "\n".join(rows)

    @staticmethod
    def _reference_lines(references: list[dict[str, object]], limit: int = 8, include_source: bool = False) -> list[str]:
        rows: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for reference in references:
            key = (
                str(reference.get("source_document_id") or ""),
                str(reference.get("document_id") or ""),
                str(reference.get("ref_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rendered = RetrievalService._reference_line(reference, include_source=include_source)
            if rendered:
                rows.append(rendered)
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _reference_line(reference: dict[str, object], include_source: bool = False) -> str:
        label = str(reference.get("marker") or f"[{reference.get('ref_id', '')}]").strip()
        number = normalize_spaces(str(reference.get("document_number") or ""))
        title = normalize_spaces(str(reference.get("title") or reference.get("raw_text") or ""))
        rendered = normalize_spaces(" ".join(part for part in [label, number, title] if part))
        source = normalize_spaces(str(reference.get("source_document_id") or ""))
        if include_source and source:
            rendered = f"{rendered} (в {source})"
        return f"- {rendered}" if rendered else ""
