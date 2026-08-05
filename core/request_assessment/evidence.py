from __future__ import annotations

from typing import Any

from .ata_context import NormalizedAtaContext
from .models import (
    ApplicabilityStatus,
    DocumentaryAssessment,
    DocumentaryAssessmentStatus,
    EvidenceRecord,
    EvidenceRelevance,
    SelectedSimilarCase,
)


def normalize_mro_kb_evidence(case_id: str | None, payload: dict[str, Any], context: NormalizedAtaContext | None = None) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        records.extend(_record_from_item(case_id, item, context) for item in evidence if isinstance(item, dict))
    sources = payload.get("sources")
    if isinstance(sources, list):
        records.extend(_record_from_item(case_id, item, context) for item in sources if isinstance(item, dict))
    return records


def assess_documentary_evidence(
    verification_required: bool,
    selected_cases: list[SelectedSimilarCase],
    evidence: list[EvidenceRecord],
    warnings: list[str] | None = None,
    unavailable: bool = False,
) -> DocumentaryAssessment:
    requested = [case.case_id for case in selected_cases]
    if not verification_required:
        return DocumentaryAssessment(status=DocumentaryAssessmentStatus.NOT_REQUIRED, verification_required=False)
    if unavailable:
        return DocumentaryAssessment(
            status=DocumentaryAssessmentStatus.UNAVAILABLE,
            verification_required=True,
            requested_case_ids=requested,
            review_items=["MRO KB unavailable; absence of documents is not inferred"],
            warnings=warnings or [],
        )
    usable = [item for item in evidence if _usable(item, requested)]
    if not evidence or not usable:
        return DocumentaryAssessment(
            status=DocumentaryAssessmentStatus.INCONCLUSIVE,
            verification_required=True,
            requested_case_ids=requested,
            review_items=["No usable documentary evidence returned"],
            warnings=warnings or [],
        )
    confirmed = [
        item for item in usable
        if item.relevance.get("status") == EvidenceRelevance.DIRECT_MATCH.value
        and item.applicability.get("status") in {ApplicabilityStatus.APPLICABLE.value, ApplicabilityStatus.POTENTIALLY_APPLICABLE.value}
    ]
    if confirmed:
        return DocumentaryAssessment(
            status=DocumentaryAssessmentStatus.CONFIRMED,
            verification_required=True,
            requested_case_ids=requested,
            confirmed_case_ids=sorted({str(item.case_id) for item in confirmed if item.case_id}),
            usable_evidence_ids=[_evidence_id(item) for item in confirmed],
            warnings=warnings or [],
        )
    if all(item.relevance.get("status") in {EvidenceRelevance.CONTEXT_ONLY.value, EvidenceRelevance.NOT_RELEVANT.value} for item in evidence):
        status = DocumentaryAssessmentStatus.NOT_CONFIRMED
    elif all(item.applicability.get("status") == ApplicabilityStatus.UNKNOWN.value for item in usable):
        status = DocumentaryAssessmentStatus.PARTIALLY_CONFIRMED
    else:
        status = DocumentaryAssessmentStatus.INCONCLUSIVE
    return DocumentaryAssessment(
        status=status,
        verification_required=True,
        requested_case_ids=requested,
        usable_evidence_ids=[_evidence_id(item) for item in usable],
        review_items=["Documentary evidence is incomplete or applicability is not confirmed"],
        warnings=warnings or [],
    )


def classify_evidence_relevance(item: dict[str, Any], expected_case_id: str | None, context: NormalizedAtaContext | None) -> EvidenceRelevance:
    descriptor = _descriptor(item)
    case = _first(item, descriptor, "case_id", "internal_case_id")
    if expected_case_id and case and case != expected_case_id:
        return EvidenceRelevance.CONTEXT_ONLY
    text = " ".join(str(value or "") for value in (item.get("section_title"), item.get("title"), item.get("snippet"), item.get("text"), item.get("content"))).lower()
    if not context:
        return EvidenceRelevance.CONTEXT_ONLY
    object_hit = any(value.lower() in text for value in context.physical_objects + context.structural_elements)
    damage_hit = any(value.lower() in text for value in context.damage_types + context.damage_descriptions)
    ata_hit = any(value.lower() in text for value in context.affected_ata)
    if expected_case_id and case == expected_case_id and object_hit and (damage_hit or ata_hit):
        return EvidenceRelevance.DIRECT_MATCH
    if object_hit or damage_hit or ata_hit:
        return EvidenceRelevance.PARTIAL_MATCH
    return EvidenceRelevance.CONTEXT_ONLY


def _record_from_item(case_id: str | None, item: dict[str, Any], context: NormalizedAtaContext | None) -> EvidenceRecord:
    descriptor = _descriptor(item)
    relevance = str(item.get("relevance_status") or item.get("relevance") or "")
    if relevance not in EvidenceRelevance.__members__ and relevance not in {value.value for value in EvidenceRelevance}:
        relevance = classify_evidence_relevance(item, case_id, context).value
    applicability = str(item.get("applicability_status") or item.get("applicability") or "")
    if applicability not in {value.value for value in ApplicabilityStatus}:
        applicability = ApplicabilityStatus.UNKNOWN.value
    return EvidenceRecord(
        case_id=_first(item, descriptor, "case_id") or case_id,
        internal_case_id=_first(item, descriptor, "internal_case_id"),
        document_id=_first(item, descriptor, "document_id", "doc_id", "id"),
        source_document_id=_first(item, descriptor, "source_document_id"),
        chunk_id=_first(item, descriptor, "chunk_id"),
        parent_id=_first(item, descriptor, "parent_id"),
        section_title=_first(item, descriptor, "section_title", "title"),
        citation_refs=[str(value) for value in (descriptor.get("citation_refs") or item.get("citation_refs") or [])] if isinstance(descriptor.get("citation_refs") or item.get("citation_refs"), list) else [],
        retrieval_mode=_first(item, descriptor, "retrieval_mode"),
        rerank_score=_float(_first(item, descriptor, "rerank_score")),
        final_score=_float(_first(item, descriptor, "final_score")),
        document_type=_first(item, descriptor, "document_type", "type"),
        revision=_first(item, descriptor, "revision"),
        effectivity=_first(item, descriptor, "effectivity"),
        aircraft_type=_first(item, descriptor, "aircraft_type"),
        aircraft_model=_first(item, descriptor, "aircraft_model"),
        msn=_first(item, descriptor, "msn"),
        ata=[str(value) for value in item.get("ata", [])] if isinstance(item.get("ata"), list) else [],
        object=_first(item, descriptor, "object", "component"),
        damage_type=_first(item, descriptor, "damage_type", "defect"),
        snippet=str(item.get("snippet") or item.get("text") or item.get("content") or "")[:1000],
        relevance={"status": relevance, "reasons": item.get("reasons") or []},
        applicability={"status": applicability, "limitations": item.get("limitations") or []},
        source_descriptor=descriptor or item,
    )


def _usable(item: EvidenceRecord, requested: list[str]) -> bool:
    if requested and item.case_id not in requested:
        return False
    return bool((item.document_id or item.source_document_id) and item.chunk_id and item.snippet.strip())


def _evidence_id(item: EvidenceRecord) -> str:
    return "|".join(str(value or "") for value in (item.case_id, item.document_id or item.source_document_id, item.chunk_id))


def _descriptor(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("source_descriptor") if isinstance(item.get("source_descriptor"), dict) else {}


def _first(item: dict[str, Any], descriptor: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            value = descriptor.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None

