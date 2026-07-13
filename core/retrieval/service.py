from __future__ import annotations

import re

from core.citations.formatting import obsidian_uri, source_label
from core.runtime_clients import ExternalReranker, OpenAICompatibleLLM, RuntimeSettings
from storage.sqlite.store import SQLiteStore


CASE_ID_RE = re.compile(r"\b(?:MRO|MP|WO|МР)-\d+\b", flags=re.IGNORECASE)
BARE_CASE_ID_RE = re.compile(r"\b(?:заявк[аеуи]?|wo|mro|mp|мр|номер|№)\s*[-№#:]?\s*(\d{2,5})\b", flags=re.IGNORECASE)
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

    def chat(self, question: str, limit: int = 6) -> dict[str, object]:
        explicit_case_id = self._extract_case_id(question)
        case_payload = self.store.fetch_case(explicit_case_id) if explicit_case_id else None
        hits = self._collect_candidates(question, limit=max(limit * 6, 36), explicit_case_id=explicit_case_id)
        if not self._is_list_query(question):
            hits = self._rerank_hits(question, hits, limit=max(limit * 6, 36))
        hits = self._select_hits(question, hits, limit=limit)
        sources: list[dict[str, object]] = []
        for hit in hits:
            note_path = str(hit.get("vault_note_path") or "")
            block_id = str(hit.get("block_id") or "")
            source_document_id = str(hit.get("source_document_id") or hit["document_id"])
            source_text = self._source_text_for_hit(hit)
            snippet_limit = 5000 if self._is_procedural_hit(hit) else 1600
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
                        "chunk_kind": str(hit.get("chunk_kind") or "chunk"),
                        "section_title": hit["section_title"],
                        "page_number": None,
                        "vault_uri": obsidian_uri(self.vault_name, note_path, block_id) if note_path else "",
                        "page_image_uri": str(hit.get("page_image_path") or ""),
                        "snapshot_id": None,
                        "retrieval_mode": "sqlite_like",
                        "link_confidence": 1.0,
                        "rerank_score": float(hit.get("rerank_score", 0.0)),
                        "final_score": float(hit.get("final_score", 0.0)),
                    },
                }
            )
        answer = self._build_answer(question, case_payload, sources)
        llm_status = "retrieval_only"
        if self._llm is not None and sources and not self._is_list_query(question):
            try:
                llm_answer = self._generate_llm_answer(question, case_payload, sources)
                if llm_answer.strip():
                    answer = llm_answer
                    llm_status = "openai_compatible"
                else:
                    llm_status = "retrieval_fallback"
            except Exception:
                llm_status = "retrieval_fallback"
        return {
            "answer": answer,
            "sources": sources,
            "evidence": sources[: min(3, len(sources))],
            "warnings": [] if sources else ["no_relevant_sources_found"],
            "snapshot_id": None,
            "llm_status": llm_status,
        }

    def health(self) -> dict[str, object]:
        return {
            "reranker": self._reranker.health() if self._reranker is not None else {"ok": False, "disabled": True},
            "llm": self._llm.health() if self._llm is not None else {"ok": False, "disabled": True},
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

    @staticmethod
    def _display_case_id(value: str) -> str:
        digits = re.findall(r"\d+", value or "")
        if not digits:
            return value
        return f"MRO-{int(digits[0]):03d}"

    def _build_answer(self, question: str, case_payload: dict[str, object] | None, sources: list[dict[str, object]]) -> str:
        lines = [f"Вопрос: {question.strip()}", ""]
        if case_payload:
            lines.append(
                f"Контекст заявки: {self._display_case_id(str(case_payload['case_id']))} / "
                f"{case_payload.get('aircraft_type') or '-'} / "
                f"MSN {case_payload.get('msn') or '-'}"
            )
            if case_payload.get("subject"):
                lines.append(f"Тема: {case_payload['subject']}")
            lines.append("")
        if sources:
            case_ids = self._case_ids_from_sources(sources)
            if self._is_list_query(question):
                lines.append("Подтвержденные заявки:")
                for case_id in case_ids:
                    lines.append(f"- {case_id}")
                lines.append("")
                lines.append("Подтверждающие фрагменты:")
            else:
                lines.append("Найдены релевантные фрагменты MRO KB:")
            for item in sources[:3]:
                lines.append(f"- {item['title']}")
                lines.append(f"  {item['snippet'][:220]}")
            lines.append("")
            lines.append("Используйте источники ниже для инженерной проверки; ответ сформирован только по найденным фрагментам.")
            lines.append("")
            lines.append(self._source_summary(sources))
            lines.append("")
            lines.append(self._sources_table(sources))
        else:
            lines.append("В найденных документах надежного ответа нет.")
        return "\n".join(lines)

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
        for query in candidate_queries[:3]:
            if explicit_case_id:
                for hit in self.store.search_text(query, limit=per_query_limit, case_ids=[explicit_case_id]):
                    chunk_id = str(hit.get("chunk_id") or "")
                    existing = merged.get(chunk_id)
                    if existing is None or float(hit.get("lexical_score", 0.0)) > float(existing.get("lexical_score", 0.0)):
                        merged[chunk_id] = hit
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
        hits.sort(key=lambda item: float(item.get("lexical_score", 0.0)), reverse=True)
        selected: list[dict[str, object]] = []
        per_case_counts: dict[str, int] = {}
        max_per_case = 6
        target = max(limit, 24)
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
            score = float(hit.get("rerank_score", hit.get("lexical_score", 0.0)))
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
        if any("корроз" in token or "corrosion" in token for token in tokens):
            if "корроз" not in haystack and "corrosion" not in haystack:
                return False
        if any("нервюр" in token or token == "rib" for token in tokens):
            if "нервюр" not in haystack and "rib" not in haystack:
                return False
        numeric_tokens = [token for token in tokens if token.isdigit()]
        for token in numeric_tokens:
            if token not in haystack:
                return False
        return True

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

        if any("нервюр" in token for token in tokens):
            add("gear rib corrosion repair")
            if any("корроз" in token for token in tokens):
                add("gear rib corrosion removal")
                add("кронштейн опоры шасси коррозия ремонт")
            add(question.replace("нервюры", "rib").replace("нервюра", "rib").replace("нервюре", "rib"))
            add("rib corrosion repair")
        add(question)
        if tokens:
            add(" ".join(tokens[:4]))
        if any("огранич" in token or "limit" in token for token in tokens):
            add("функции и ограничения ВС")
            add("оценка влияния изменения на ВС")
            add("не оказывает влияния на функции и ограничения")
            add("ограничения летной годности")
            add("airworthiness limitations")
            add("instructions for continued airworthiness")
            add("эксплуатация допускается")
        if any("решен" in token or "финаль" in token or "итог" in token for token in tokens):
            add("подтверждение выполнения ремонта")
            add("результат классификации")
            add("заключение")
            add("repair confirmation")
        if any("корроз" in token for token in tokens):
            add("corrosion repair")
            add("corrosion removal")
        if any(token.startswith("ремонт") for token in tokens):
            add("repair accomplishment instruction")
            add("ремонт repair")
        ranked_tokens = sorted(tokens, key=lambda token: (-len(token), token))
        for token in ranked_tokens[:4]:
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

    def _source_text_for_hit(self, hit: dict[str, object]) -> str:
        text = normalize_spaces(str(hit.get("text") or ""))
        if not self._is_procedural_hit(hit):
            return text
        section_title = str(hit.get("section_title") or "")
        document_id = str(hit.get("document_id") or "")
        if not section_title or not document_id:
            return text
        section_chunks = self.store.fetch_section_chunks(document_id, section_title, limit=80)
        section_texts: list[str] = []
        seen: set[str] = set()
        for chunk in section_chunks:
            chunk_text = normalize_spaces(str(chunk.get("text") or ""))
            if not chunk_text or chunk_text in seen:
                continue
            seen.add(chunk_text)
            section_texts.append(chunk_text)
        return "\n".join(section_texts) or text

    @staticmethod
    def _is_procedural_hit(hit: dict[str, object]) -> bool:
        haystack = " ".join(
            [
                str(hit.get("section_title") or ""),
                str(hit.get("section_label") or ""),
                str(hit.get("document_family") or ""),
                str(hit.get("search_text") or ""),
                str(hit.get("text") or ""),
            ]
        ).lower()
        return (
            "ремонт" in haystack
            or "repair" in haystack
            or "accomplishment" in haystack
            or "инструкция по выполнению" in haystack
            or "removal of corrosion" in haystack
        )

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
        text = str(hit.get("text") or "").lower()
        if "ремонт" in text and any(token in {"ремонт", "repair", "repairing", "ремонтировались"} for token in tokens):
            bonus += 0.18
        if "корроз" in text and any("корроз" in token or "corrosion" in token for token in tokens):
            bonus += 0.18
        if ("removal of corrosion" in haystack or "удал" in haystack and "корроз" in haystack) and any(
            "корроз" in token or "corrosion" in token for token in tokens
        ):
            bonus += 0.45
        if "предкрыл" in text and any("предкрыл" in token or "slat" in token for token in tokens):
            bonus += 0.18
        if "балк" in text and any("балк" in token or "beam" in token or "floor" in token for token in tokens):
            bonus += 0.18
        if any("огранич" in token or "limit" in token for token in tokens):
            document_family = str(hit.get("document_family") or "").lower()
            if document_family == "opi":
                bonus += 1.2
            if "оценка влияния" in section_title or "функции и ограничения" in haystack:
                bonus += 1.4
            if "не оказывает влияния" in haystack and "огранич" in haystack:
                bonus += 1.0
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
        if section_title == "document_intro":
            penalty += 0.45
        lowered_title = section_title.lower()
        lowered_text = text.lower()
        if "подтверждение выполнения ремонта" in lowered_title or "confirmation of repair" in lowered_text:
            penalty += 0.65
        if "mro@ikarcenter.ru" in lowered_text:
            penalty += 0.45
        if "инструкция разделена на шаги" in lowered_text or "implementation of the technical disposition is divided into steps" in lowered_text:
            penalty += 0.9
        if (
            "в случае возникновения отклонений" in lowered_text
            or "в случае обнаружения несоответствий" in lowered_text
            or "форма оценки качества инструкции" in lowered_text
            or "in case of deviations" in lowered_text
            or "in case of discrepancies" in lowered_text
        ):
            penalty += 0.9
        if len(text) < 80:
            penalty += 0.2
        if chunk_kind == "table" and len(text) < 180:
            penalty += 0.1
        if "part number" in text.lower() and "description" in text.lower() and len(text) < 260:
            penalty += 0.25
        if "таблица соответствия" in lowered_title:
            penalty += 1.2
        return penalty

    @staticmethod
    def _is_low_value_hit(hit: dict[str, object]) -> bool:
        text = normalize_spaces(str(hit.get("text") or ""))
        if not text:
            return True
        if "<!-- image -->" in text and len(text) < 600:
            return True
        if str(hit.get("section_title") or "") == "document_intro" and len(text) < 160:
            return True
        return False

    def _generate_llm_answer(
        self,
        question: str,
        case_payload: dict[str, object] | None,
        sources: list[dict[str, object]],
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
        lines.append("Найденные фрагменты:")
        for idx, source in enumerate(sources[:4], start=1):
            descriptor = source.get("source_descriptor") or {}
            lines.append(
                f"[{idx}]\n"
                f"case_id={descriptor.get('case_id', '')}\n"
                f"document={descriptor.get('source_document_id', '')}\n"
                f"section={descriptor.get('section_title', '')}\n"
                f"chunk_id={descriptor.get('chunk_id', '')}\n"
                f"snippet={str(source['snippet'])[:1200]}\n"
            )
        user_prompt = "\n".join(lines)
        answer = self._llm.chat(system_prompt, user_prompt).strip()
        if not answer:
            return answer
        suffix = self._source_summary(sources)
        appendix = f"{suffix}\n\n{self._sources_table(sources)}" if suffix else self._sources_table(sources)
        return f"{answer}\n\n{appendix}"

    @staticmethod
    def _score_from_source(source: dict[str, object]) -> float:
        descriptor = source.get("source_descriptor") or {}
        value = descriptor.get("rerank_score")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _sources_table(self, sources: list[dict[str, object]]) -> str:
        rows = [
            "### Источники",
            "",
            "| Документ | Раздел | Текст чанка | Ссылка |",
            "|---|---|---|---|",
        ]
        for source in sources[:6]:
            descriptor = source.get("source_descriptor") or {}
            link_target = str(descriptor.get("vault_uri") or "").strip()
            link = f"[Открыть]({link_target})" if link_target else ""
            chunk_text = normalize_spaces(str(source.get("snippet") or ""))
            if len(chunk_text) > 220:
                chunk_text = f"{chunk_text[:217]}..."
            chunk_text = chunk_text.replace("|", "\\|")
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
