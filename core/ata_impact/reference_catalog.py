from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from core.config import DATA_RUNTIME_ROOT

from .identifiers import normalize_ata


DEFAULT_REFERENCE_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "gost_18675_2012_appendix_a.json"
)
DEFAULT_VECTOR_CACHE_PATH = DATA_RUNTIME_ROOT / "ata_gost_reference_vectors.json"
_QUERY_STOP_TOKENS = frozenset(
    {
        "airbus",
        "aircraft",
        "авиационной",
        "воздушного",
        "воздушное",
        "время",
        "данные",
        "изделия",
        "между",
        "обнаружена",
        "обнаружено",
        "оборудование",
        "повреждение",
        "повреждения",
        "проведения",
        "работы",
        "районе",
        "система",
        "системы",
        "часть",
    }
)
_CONTROLLED_CHAPTER_HINTS: dict[str, dict[str, object]] = {
    "ATA 25": {
        "positive": (
            "салон",
            "пассажирский салон",
            "кресло",
            "кресла",
            "сиденье",
            "обивка",
            "чехол",
            "чехлы",
            "мягкость",
            "интерьер",
            "cabin",
            "seat",
            "seat cover",
            "interior",
        ),
        "negative": (),
        "reason": "controlled_reference_hint: cabin/interior equipment",
    },
    "ATA 44": {
        "positive": (
            "развлечение",
            "видео",
            "музыка",
            "мультимедиа",
            "пассажирская связь",
            "ife",
            "entertainment",
            "connectivity",
        ),
        "negative": ("чехол", "чехлы", "кресло", "сиденье", "seat cover"),
        "reason": "controlled_reference_hint: passenger entertainment/information system",
    },
    "ATA 57": {
        "positive": (
            "крыло",
            "центроплан",
            "отъемная часть крыла",
            "нервюра",
            "нервюры",
            "rib",
            "rib5",
            "лонжерон",
            "spar",
            "закрылок",
            "предкрылок",
            "wing",
            "flap",
            "slat",
        ),
        "negative": (),
        "reason": "controlled_reference_hint: wing structure",
    },
    "ATA 55": {
        "positive": (
            "оперение",
            "стабилизатор",
            "киль",
            "руль высоты",
            "руль направления",
            "empennage",
            "stabilizer",
            "rudder",
            "elevator",
            "tailplane",
        ),
        "negative": ("rib5",),
        "reason": "controlled_reference_hint: empennage structure",
    },
}


@dataclass(frozen=True, slots=True)
class AtaClassificationReference:
    reference_id: str
    ata: str
    parent_ata: str
    title: str
    description: str
    source_section: str
    allowed_mapping_categories: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        return {
            "reference_id": self.reference_id,
            "ata": self.ata,
            "parent_ata": self.parent_ata,
            "title": self.title,
            "description": self.description,
            "source_section": self.source_section,
            "source_kind": "classification_standard",
            "allowed_mapping_categories": list(
                self.allowed_mapping_categories
            ),
        }


class AtaClassificationReferenceCatalog:
    """Civil ATA classification reference, separate from certificate scope."""

    def __init__(
        self,
        path: Path | None = None,
        vector_cache_path: Path | None = None,
    ) -> None:
        self.path = path or DEFAULT_REFERENCE_PATH
        self.vector_cache_path = (
            vector_cache_path or DEFAULT_VECTOR_CACHE_PATH
        )
        self.embedding_url = os.getenv(
            "MRO_KB_OLLAMA_URL",
            "http://127.0.0.1:11434",
        ).rstrip("/")
        self.embedding_model = os.getenv(
            "MRO_KB_EMBEDDING_MODEL",
            "bge-m3:latest",
        )
        vector_flag = os.getenv("MRO_KB_ATA_REFERENCE_VECTORS_ENABLED")
        self.vector_search_enabled = (
            vector_flag.strip().lower() in {"1", "true", "yes", "on"}
            if vector_flag is not None
            else False
        )
        self.source_document = "ГОСТ 18675-2012"
        self.source_section = "Приложение А (рекомендуемое)"
        self.revision = "2012"
        self.source_file_sha256 = ""
        self.entries: list[AtaClassificationReference] = []
        self.chapter_role_policy: dict[str, tuple[str, ...]] = {}
        self.load_errors: list[str] = []
        self._load()
        self._by_ata: dict[str, list[AtaClassificationReference]] = {}
        for entry in self.entries:
            self._by_ata.setdefault(entry.ata, []).append(entry)
        self._vectors = self._load_vectors()

    @property
    def available(self) -> bool:
        return bool(self.entries) and not self.load_errors

    def context(
        self,
        request: str,
        engineering_facts: dict[str, object],
        limit: int = 8,
    ) -> dict[str, object]:
        if not self.available:
            return {
                "status": "unavailable",
                "source": self.source_document,
                "section": self.source_section,
                "revision": self.revision,
                "source_file_sha256": self.source_file_sha256,
                "chapter_index": [],
                "relevant_definitions": [],
                "retrieved_reference_ids": [],
                "errors": list(self.load_errors or ["classification_reference_empty"]),
            }
        query = _query_text(request, engineering_facts)
        try:
            query_vector = (
                self._embed_query(query)
                if self.vector_search_enabled and self._vectors
                else []
            )
        except Exception:
            query_vector = []
        if query_vector:
            relevant = self._hybrid_rank(query, query_vector, limit)
            retrieval_mode = "hybrid_vector_lexical"
        else:
            relevant = self._rank(query, limit)
            retrieval_mode = "lexical_fallback"
        chapter_index = self._chapter_index(relevant)
        return {
            "status": "completed",
            "source": self.source_document,
            "section": self.source_section,
            "revision": self.revision,
            "source_file_sha256": self.source_file_sha256,
            "chapter_index": chapter_index,
            "relevant_definitions": [entry.as_dict() for entry in relevant],
            "retrieved_reference_ids": [entry.reference_id for entry in relevant],
            "retrieval_mode": retrieval_mode,
            "errors": [],
        }

    def build_vector_index(self, batch_size: int = 32) -> dict[str, object]:
        vectors: dict[str, list[float]] = {}
        batch_size = max(1, min(int(batch_size), 64))
        chapters = self._chapter_entries()
        for start in range(0, len(chapters), batch_size):
            entries = chapters[start : start + batch_size]
            embedded = self._embed_batch(
                [self._chapter_text(entry.ata) for entry in entries],
                timeout=120,
            )
            if len(embedded) != len(entries):
                raise RuntimeError(
                    f"ATA embedding batch failed at offset {start}"
                )
            for entry, vector in zip(entries, embedded):
                vectors[entry.ata] = vector
        payload = {
            "schema_version": 2,
            "granularity": "ata_chapter",
            "model": self.embedding_model,
            "source_file_sha256": self.source_file_sha256,
            "vector_count": len(vectors),
            "vectors": vectors,
        }
        self.vector_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.vector_cache_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.vector_cache_path)
        self._vectors = vectors
        return {
            "status": "completed",
            "model": self.embedding_model,
            "vectors": len(vectors),
            "cache": str(self.vector_cache_path),
        }

    def context_for_atas(self, ata_codes: list[str]) -> dict[str, object]:
        selected: list[AtaClassificationReference] = []
        seen: set[str] = set()
        for raw_ata in ata_codes:
            ata = normalize_ata(raw_ata)
            chapter = normalize_ata(ata[:6]) if ata else ""
            entries = [
                *self._by_ata.get(ata, []),
                *self._by_ata.get(chapter, []),
            ]
            for entry in entries:
                if entry.reference_id not in seen:
                    selected.append(entry)
                    seen.add(entry.reference_id)
        return {
            "source": self.source_document,
            "section": self.source_section,
            "revision": self.revision,
            "chapter_index": self._chapter_index(selected),
            "relevant_definitions": [
                entry.as_dict() for entry in selected
            ],
        }

    def propose_mapping_candidates(
        self,
        request: str,
        engineering_facts: dict[str, object],
    ) -> dict[str, list[dict[str, object]]]:
        proposals: dict[str, list[dict[str, object]]] = {
            "object_ata": [],
            "structural_ata": [],
            "location_context_ata": [],
            "interface_ata_hypotheses": [],
            "procedure_ata_hypotheses": [],
            "user_declared_ata": [],
        }
        if not self.available:
            return proposals
        for entity in _affected_entities(engineering_facts):
            category = "structural_ata" if entity["entity_type"] == "structure" else "object_ata"
            scored = self._score_chapters_for_entity(request, engineering_facts, entity, category)
            if not scored:
                continue
            score, ata, reason = scored[0]
            if score < 2.0:
                continue
            proposals[category].append(
                {
                    "ata": ata,
                    "entity_id": entity["entity_id"],
                    "confidence": min(0.9, max(0.55, score / 10.0)),
                    "reason": reason,
                    "source_fragment": entity["name"],
                }
            )
        return proposals

    def attach_references(
        self,
        mapping: dict[str, list[dict[str, object]]],
    ) -> list[str]:
        ungrounded: list[str] = []
        for candidates in mapping.values():
            for candidate in candidates:
                ata = normalize_ata(candidate.get("ata"))
                references = self.reference_ids_for_ata(ata)
                candidate["classification_reference_ids"] = references
                if not references and candidate.get("candidate_id"):
                    ungrounded.append(str(candidate["candidate_id"]))
        return ungrounded

    def enforce_mapping_roles(
        self,
        mapping: dict[str, list[dict[str, object]]],
    ) -> list[str]:
        warnings: list[str] = []
        for category, candidates in mapping.items():
            accepted: list[dict[str, object]] = []
            for candidate in candidates:
                ata = normalize_ata(candidate.get("ata"))
                chapter = normalize_ata(ata[:6]) if ata else ""
                allowed = self.chapter_role_policy.get(chapter, ())
                if allowed and category not in allowed:
                    warnings.append(
                        "mapping_role_candidate_removed:"
                        f"{category}:{ata}:allowed={','.join(allowed)}"
                    )
                    continue
                accepted.append(candidate)
            mapping[category] = accepted
        return warnings

    def _score_chapters_for_entity(
        self,
        request: str,
        engineering_facts: dict[str, object],
        entity: dict[str, str],
        category: str,
    ) -> list[tuple[float, str, str]]:
        query = _entity_query_text(request, engineering_facts, entity)
        query_tokens = Counter(_tokens(query))
        if not query_tokens:
            return []
        scored: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for entry in self.entries:
            allowed = self.chapter_role_policy.get(entry.parent_ata, ())
            if allowed and category not in allowed:
                continue
            entry_tokens = Counter(_tokens(_reference_text(entry)))
            lexical = 0.0
            for token, count in query_tokens.items():
                matches = entry_tokens.get(token, 0)
                if not matches and len(token) >= 6:
                    prefix = token[:5]
                    matches = sum(
                        value
                        for ref_token, value in entry_tokens.items()
                        if len(ref_token) >= 6 and ref_token.startswith(prefix)
                    )
                if matches:
                    lexical += min(matches, 3) * min(count, 2)
            if lexical:
                scored[entry.parent_ata] = scored.get(entry.parent_ata, 0.0) + lexical
                reasons.setdefault(entry.parent_ata, []).append(
                    f"ГОСТ lexical match: {entry.ata} {entry.title}"
                )
        hint_scores = _controlled_hint_scores(query)
        for ata, hint in hint_scores.items():
            scored[ata] = scored.get(ata, 0.0) + hint[0]
            reasons.setdefault(ata, []).append(hint[1])
        ranked = [
            (
                score,
                ata,
                "; ".join(dict.fromkeys(reasons.get(ata, [])))[0:500],
            )
            for ata, score in scored.items()
        ]
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked

    def reference_ids_for_ata(self, ata: str) -> list[str]:
        normalized = normalize_ata(ata)
        exact = self._by_ata.get(normalized, [])
        if exact:
            return [entry.reference_id for entry in exact]
        chapter = normalize_ata(normalized[:6]) if normalized else ""
        return [
            entry.reference_id
            for entry in self._by_ata.get(chapter, [])
            if entry.ata == entry.parent_ata
        ]

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.load_errors.append("classification_reference_file_missing")
            return
        except (OSError, json.JSONDecodeError):
            self.load_errors.append("classification_reference_file_invalid")
            return
        if not isinstance(payload, dict) or payload.get("aviation_scope") != "civil":
            self.load_errors.append("classification_reference_schema_invalid")
            return
        self.source_document = str(payload.get("source_document") or self.source_document)
        self.source_section = str(payload.get("source_section") or self.source_section)
        self.revision = str(payload.get("revision") or self.revision)
        self.source_file_sha256 = str(
            payload.get("source_file_sha256") or ""
        ).strip()
        raw_role_policy = payload.get("chapter_role_policy")
        if isinstance(raw_role_policy, dict):
            self.chapter_role_policy = {
                normalize_ata(chapter): tuple(
                    str(category)
                    for category in categories
                    if isinstance(category, str)
                )
                for chapter, categories in raw_role_policy.items()
                if isinstance(categories, list)
                and normalize_ata(chapter)
            }
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            self.load_errors.append("classification_reference_schema_invalid")
            return
        seen: set[str] = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                self.load_errors.append("classification_reference_entry_invalid")
                continue
            reference_id = str(raw.get("reference_id") or "").strip()
            ata = normalize_ata(raw.get("ata"))
            parent_ata = normalize_ata(raw.get("parent_ata"))
            title = str(raw.get("title") or "").strip()
            if (
                not reference_id
                or reference_id in seen
                or not ata
                or not parent_ata
                or not title
                or str(raw.get("source_kind") or "") != "classification_standard"
            ):
                self.load_errors.append("classification_reference_entry_invalid")
                continue
            seen.add(reference_id)
            self.entries.append(
                AtaClassificationReference(
                    reference_id=reference_id,
                    ata=ata,
                    parent_ata=parent_ata,
                    title=title,
                    description=str(raw.get("description") or "").strip(),
                    source_section=str(raw.get("source_section") or self.source_section),
                    allowed_mapping_categories=self.chapter_role_policy.get(
                        parent_ata,
                        (),
                    ),
                )
            )

    def _chapter_index(
        self,
        relevant: list[AtaClassificationReference],
    ) -> list[dict[str, object]]:
        relevant_chapters = {entry.parent_ata for entry in relevant}
        chapters: dict[str, list[AtaClassificationReference]] = {}
        for entry in self.entries:
            if (
                entry.ata == entry.parent_ata
                and entry.ata in relevant_chapters
            ):
                chapters.setdefault(entry.ata, []).append(entry)
        return [
            {
                "ata": ata,
                "title": " / ".join(dict.fromkeys(item.title for item in entries)),
                "reference_ids": [item.reference_id for item in entries],
                "allowed_mapping_categories": list(
                    dict.fromkeys(
                        category
                        for item in entries
                        for category in item.allowed_mapping_categories
                    )
                ),
            }
            for ata, entries in sorted(chapters.items())
        ]

    def _rank(self, query: str, limit: int) -> list[AtaClassificationReference]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        documents = [
            _tokens(f"{entry.title} {entry.title} {entry.description}")
            for entry in self.entries
        ]
        document_frequency: Counter[str] = Counter()
        for tokens in documents:
            document_frequency.update(set(tokens))
        query_counts = Counter(query_tokens)
        scored: list[tuple[float, str, AtaClassificationReference]] = []
        total = len(documents)
        for entry, tokens in zip(self.entries, documents):
            counts = Counter(tokens)
            score = 0.0
            for query_token, query_count in query_counts.items():
                matches = counts.get(query_token, 0)
                if not matches and len(query_token) >= 6:
                    prefix = query_token[:5]
                    matches = sum(
                        count
                        for token, count in counts.items()
                        if len(token) >= 6 and token.startswith(prefix)
                    )
                if matches:
                    frequency = document_frequency.get(query_token, 0)
                    inverse_frequency = math.log((total + 1) / (frequency + 1)) + 1
                    score += min(matches, 3) * inverse_frequency * min(query_count, 2)
            if score:
                scored.append((score, entry.reference_id, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected: list[AtaClassificationReference] = []
        selected_per_chapter: Counter[str] = Counter()
        for _, _, entry in scored:
            if selected_per_chapter[entry.parent_ata] >= 2:
                continue
            selected.append(entry)
            selected_per_chapter[entry.parent_ata] += 1
            if len(selected) >= max(1, limit):
                break
        return selected

    def _rank_vectors(
        self,
        query_vector: list[float],
        limit: int,
    ) -> list[AtaClassificationReference]:
        scored = [
            (_cosine(query_vector, vector), entry.ata, entry)
            for entry in self._chapter_entries()
            if (vector := self._vectors.get(entry.ata))
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [entry for _, _, entry in scored[: max(1, limit)]]

    def _hybrid_rank(
        self,
        query: str,
        query_vector: list[float],
        limit: int,
    ) -> list[AtaClassificationReference]:
        vector_entries = self._rank_vectors(query_vector, 4)
        lexical_entries = self._rank(query, 16)
        lexical_chapters: list[str] = []
        for entry in lexical_entries:
            if entry.parent_ata not in lexical_chapters:
                lexical_chapters.append(entry.parent_ata)
            if len(lexical_chapters) >= 6:
                break
        scores: dict[str, float] = {}
        for rank, entry in enumerate(vector_entries, start=1):
            scores[entry.parent_ata] = scores.get(entry.parent_ata, 0.0) + (
                1.0 / (10 + rank)
            )
        for rank, chapter in enumerate(lexical_chapters, start=1):
            scores[chapter] = scores.get(chapter, 0.0) + (
                1.5 / (10 + rank)
            )
        chapters = {entry.ata: entry for entry in self._chapter_entries()}
        ranked = sorted(
            (
                (-score, chapter, chapters[chapter])
                for chapter, score in scores.items()
                if chapter in chapters
            )
        )
        return [entry for _, _, entry in ranked[: max(1, limit)]]

    def _chapter_entries(self) -> list[AtaClassificationReference]:
        result: dict[str, AtaClassificationReference] = {}
        for entry in self.entries:
            if entry.ata == entry.parent_ata:
                result.setdefault(entry.ata, entry)
        return [result[ata] for ata in sorted(result)]

    def _chapter_text(self, chapter: str) -> str:
        entries = [
            entry for entry in self.entries if entry.parent_ata == chapter
        ]
        return " ".join(_reference_text(entry) for entry in entries)[:6000]

    def _load_vectors(self) -> dict[str, list[float]]:
        try:
            payload = json.loads(
                self.vector_cache_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, dict)
            or
            payload.get("schema_version") != 2
            or payload.get("granularity") != "ata_chapter"
            or payload.get("model") != self.embedding_model
            or payload.get("source_file_sha256") != self.source_file_sha256
            or not isinstance(payload.get("vectors"), dict)
        ):
            return {}
        vectors: dict[str, list[float]] = {}
        for reference_id, vector in payload["vectors"].items():
            if not isinstance(vector, list) or not vector:
                continue
            try:
                numeric = [float(value) for value in vector]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in numeric):
                vectors[str(reference_id)] = numeric
        return vectors

    def _embed_query(self, text: str) -> list[float]:
        embedded = self._embed_batch([text[:6000]], timeout=10)
        return embedded[0] if len(embedded) == 1 else []

    def _embed_batch(
        self,
        texts: list[str],
        *,
        timeout: int,
    ) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self.embedding_url}/api/embed",
            data=json.dumps(
                {"model": self.embedding_model, "input": texts}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            return []
        result: list[list[float]] = []
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                return []
            try:
                numeric = [float(value) for value in vector]
            except (TypeError, ValueError):
                return []
            if not all(math.isfinite(value) for value in numeric):
                return []
            result.append(numeric)
        return result


def _query_text(request: str, facts: dict[str, object]) -> str:
    values = [request]
    damaged_ids = {
        str(item.get("affected_entity_id") or "")
        for item in facts.get("damage", [])
        if isinstance(item, dict) and item.get("affected_entity_id")
    }
    if not damaged_ids:
        damaged_ids = {
            str(item.get("id") or "")
            for key in ("physical_objects", "structural_elements")
            for item in facts.get(key, [])
            if isinstance(item, dict)
            and (
                item.get("damage_confirmed") is True
                or str(item.get("involvement") or "").lower()
                in {
                    "damaged",
                    "changed",
                    "modified",
                    "removed",
                    "replaced",
                    "repair",
                    "repaired",
                    "work_target",
                }
            )
        }
    related_ids = set(damaged_ids)
    for relation in facts.get("relations", []):
        if not isinstance(relation, dict):
            continue
        source = str(relation.get("source_entity_id") or "")
        target = str(relation.get("target_entity_id") or "")
        if source in damaged_ids or target in damaged_ids:
            related_ids.update({source, target})
    for key in ("physical_objects", "functional_purposes", "locations", "structural_elements"):
        items = facts.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("object_id") or "")
            if damaged_ids and item_id and item_id not in related_ids:
                continue
            values.extend(
                str(value)
                for field, value in item.items()
                if isinstance(value, (str, int, float))
                and field not in {"id", "object_id", "confidence"}
            )
    for item in facts.get("damage", []):
        if isinstance(item, dict):
            values.extend(
                str(value)
                for field, value in item.items()
                if isinstance(value, (str, int, float))
                and field not in {"affected_entity_id"}
            )
    for item in facts.get("relations", []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_entity_id") or "")
        target = str(item.get("target_entity_id") or "")
        if damaged_ids and source not in related_ids and target not in related_ids:
            continue
        values.extend(
            str(value)
            for field, value in item.items()
            if isinstance(value, (str, int, float))
            and field
            in {"relation", "evidence_type", "interface_basis"}
        )
    return " ".join(values)


def _affected_entities(facts: dict[str, object]) -> list[dict[str, str]]:
    damaged_ids = {
        str(item.get("affected_entity_id") or "")
        for item in facts.get("damage", [])
        if isinstance(item, dict) and item.get("affected_entity_id")
    }
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entity_type in (
        ("physical_objects", "object"),
        ("structural_elements", "structure"),
    ):
        items = facts.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("id") or "")
            involvement = str(item.get("involvement") or "").lower()
            affected = (
                entity_id in damaged_ids
                or item.get("damage_confirmed") is True
                or involvement
                in {
                    "damaged",
                    "changed",
                    "modified",
                    "removed",
                    "replaced",
                    "repair",
                    "repaired",
                    "work_target",
                }
            )
            identity = (entity_type, entity_id)
            if not entity_id or not affected or identity in seen:
                continue
            seen.add(identity)
            result.append(
                {
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "name": str(item.get("name") or item.get("original_text") or ""),
                }
            )
    return result


def _entity_query_text(request: str, facts: dict[str, object], entity: dict[str, str]) -> str:
    entity_id = entity["entity_id"]
    values = [request, entity.get("name", "")]
    for key in ("physical_objects", "structural_elements", "locations"):
        items = facts.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or str(item.get("id") or "") != entity_id:
                continue
            values.extend(
                str(value)
                for field, value in item.items()
                if isinstance(value, (str, int, float)) and field not in {"id", "confidence"}
            )
    for item in facts.get("functional_purposes", []):
        if isinstance(item, dict) and str(item.get("object_id") or "") == entity_id:
            values.extend(
                str(value)
                for field, value in item.items()
                if isinstance(value, (str, int, float)) and field not in {"object_id", "confidence"}
            )
    for item in facts.get("damage", []):
        if isinstance(item, dict) and str(item.get("affected_entity_id") or "") == entity_id:
            values.extend(
                str(value)
                for field, value in item.items()
                if isinstance(value, (str, int, float)) and field != "affected_entity_id"
            )
    return " ".join(values)


def _controlled_hint_scores(query: str) -> dict[str, tuple[float, str]]:
    normalized = query.lower().replace("ё", "е")
    result: dict[str, tuple[float, str]] = {}
    for ata, config in _CONTROLLED_CHAPTER_HINTS.items():
        positive = tuple(str(item).lower() for item in config.get("positive", ()) if str(item))
        negative = tuple(str(item).lower() for item in config.get("negative", ()) if str(item))
        score = 0.0
        for term in positive:
            if term and term in normalized:
                score += 12.0 if " " in term else 8.0
        for term in negative:
            if term and term in normalized:
                score -= 8.0
        if score > 0:
            result[ata] = (score, str(config.get("reason") or "controlled_reference_hint"))
    return result


def _tokens(value: str) -> list[str]:
    normalized = value.lower().replace("ё", "е")
    return [
        token
        for token in re.findall(r"[a-zа-я0-9]+", normalized)
        if (
            len(token) >= 3
            and not token.isdigit()
            and token not in _QUERY_STOP_TOKENS
        )
    ]


def _reference_text(entry: AtaClassificationReference) -> str:
    return (
        f"{entry.ata}. {entry.title}. {entry.description}. "
        f"Родительская глава: {entry.parent_ata}."
    )


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return -1.0
    return sum(a * b for a, b in zip(left, right)) / denominator
