from __future__ import annotations

from typing import Any

from .models import ApplicabilityStatus, EvidenceRecord, EvidenceRelevance


def normalize_mro_kb_evidence(case_id: str | None, payload: dict[str, Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                records.append(_record_from_item(case_id, item))
    sources = payload.get("sources")
    if not records and isinstance(sources, list):
        for item in sources:
            if isinstance(item, dict):
                records.append(_record_from_item(case_id, item))
    return records


def _record_from_item(case_id: str | None, item: dict[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        case_id=str(item.get("case_id") or case_id or "") or None,
        document_id=_maybe(item, "document_id", "doc_id", "id"),
        chunk_id=_maybe(item, "chunk_id"),
        section_title=_maybe(item, "section_title", "title"),
        document_type=_maybe(item, "document_type", "type"),
        revision=_maybe(item, "revision"),
        aircraft_type=_maybe(item, "aircraft_type"),
        aircraft_model=_maybe(item, "aircraft_model"),
        msn=_maybe(item, "msn"),
        ata=[str(value) for value in item.get("ata", [])] if isinstance(item.get("ata"), list) else [],
        object=_maybe(item, "object", "component"),
        damage_type=_maybe(item, "damage_type", "defect"),
        snippet=str(item.get("snippet") or item.get("text") or item.get("content") or "")[:1000],
        relevance={"status": item.get("relevance_status") or EvidenceRelevance.PARTIAL_MATCH.value, "reasons": item.get("reasons") or []},
        applicability={"status": item.get("applicability_status") or ApplicabilityStatus.UNKNOWN.value, "limitations": item.get("limitations") or []},
        source_descriptor=item,
    )


def _maybe(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if item.get(key):
            return str(item[key])
    return None

