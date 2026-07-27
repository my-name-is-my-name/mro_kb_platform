from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .identifiers import normalize_ata
from .models import MAPPING_CATEGORIES

CONTROLLED_TRUST_LEVELS = frozenset({"controlled_oem", "approved_data"})
REQUIRED_DOCUMENT_FIELDS = (
    "document_id",
    "document_type",
    "revision",
    "effectivity",
    "section_reference",
)


def controlled_candidate_ids(
    document: dict[str, object],
    valid_candidates: set[str] | dict[str, dict[str, object]] | None = None,
) -> set[str]:
    records = document.get("confirmed_candidates")
    if not isinstance(records, list):
        return set()
    result: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not _valid_confirmation_record(record):
            continue
        candidate_id = str(record["candidate_id"])
        if isinstance(valid_candidates, set) and candidate_id not in valid_candidates:
            continue
        if isinstance(valid_candidates, dict):
            candidate = valid_candidates.get(candidate_id)
            if candidate is None or not _record_matches_candidate(record, candidate):
                continue
        result.add(candidate_id)
    return result


def is_controlled_evidence_document(
    document: dict[str, object],
    valid_candidates: set[str] | dict[str, dict[str, object]] | None = None,
) -> bool:
    candidate_ids = controlled_candidate_ids(document, valid_candidates)
    return (
        str(document.get("trust_level") or "").lower() in CONTROLLED_TRUST_LEVELS
        and document.get("applicable") is True
        and document.get("current_revision") is True
        and str(document.get("verification_status") or "").lower() == "confirmed"
        and all(
            isinstance(document.get(key), str)
            and bool(str(document.get(key)).strip())
            for key in REQUIRED_DOCUMENT_FIELDS
        )
        and bool(candidate_ids)
    )


def _valid_confirmation_record(record: dict[str, object]) -> bool:
    category = str(record.get("category") or "")
    return (
        bool(str(record.get("candidate_id") or "").strip())
        and bool(normalize_ata(record.get("ata")))
        and category in MAPPING_CATEGORIES
        and category not in {"location_context_ata", "user_declared_ata"}
        and bool(record.get("entity_id") or record.get("relation_id"))
        and str(record.get("verification_status") or "").lower() == "confirmed"
        and bool(str(record.get("confirmed_claim") or "").strip())
    )


def _record_matches_candidate(
    record: dict[str, object],
    candidate: dict[str, object],
) -> bool:
    category = str(candidate.get("mapping_category") or candidate.get("category") or "")
    if (
        normalize_ata(record.get("ata")) != normalize_ata(candidate.get("ata"))
        or str(record.get("category") or "") != category
    ):
        return False
    if category in {"object_ata", "structural_ata"}:
        return bool(candidate.get("entity_id")) and record.get("entity_id") == candidate.get("entity_id")
    if category == "interface_ata_hypotheses":
        if candidate.get("relation_id"):
            return record.get("relation_id") == candidate.get("relation_id")
        return bool(candidate.get("entity_id")) and record.get("entity_id") == candidate.get("entity_id")
    return any(
        candidate.get(key) not in (None, "")
        and record.get(key) == candidate.get(key)
        for key in ("entity_id", "relation_id")
    )


@dataclass(slots=True)
class EvidenceSearchResult:
    status: str
    documents: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "documents": self.documents, "warnings": self.warnings}


class AtaEvidenceRetriever(Protocol):
    def search(
        self,
        request: str,
        engineering_facts: dict[str, object],
        ata_candidates: list[str],
        mapping_candidates: list[dict[str, object]],
        aircraft: dict[str, object],
        document_types: list[str],
        limit: int,
    ) -> EvidenceSearchResult:
        ...


class NullAtaEvidenceRetriever:
    def search(
        self,
        request: str,
        engineering_facts: dict[str, object],
        ata_candidates: list[str],
        mapping_candidates: list[dict[str, object]],
        aircraft: dict[str, object],
        document_types: list[str],
        limit: int,
    ) -> EvidenceSearchResult:
        return EvidenceSearchResult(
            status="not_configured",
            warnings=["OEM document corpus is not configured"],
        )


class LegacyEvidenceRetrieverAdapter:
    """Temporary adapter for the current SQLite/T-Search retrieval contract."""

    def __init__(self, retriever: object) -> None:
        self.retriever = retriever

    def search(
        self,
        request: str,
        engineering_facts: dict[str, object],
        ata_candidates: list[str],
        mapping_candidates: list[dict[str, object]],
        aircraft: dict[str, object],
        document_types: list[str],
        limit: int,
    ) -> EvidenceSearchResult:
        try:
            result = self.retriever.retrieve(  # type: ignore[attr-defined]
                request,
                {
                    "ata_codes": ata_candidates,
                    "aircraft_type": aircraft.get("family") or aircraft.get("model") or "",
                    "document_types": document_types,
                    # Candidate IDs are request-scoped.  The controlled OEM
                    # verifier must receive the exact validated records in
                    # order to emit confirmation records that can transition
                    # those candidates, rather than trying to infer identity
                    # from ATA strings after retrieval.
                    "mapping_candidates": mapping_candidates,
                    "controlled_only": True,
                    "purpose": "ata_document_verification",
                },
                limit=limit,
            )
            return EvidenceSearchResult(
                status=str(result.get("status") or "completed"),
                documents=[item for item in result.get("documents", []) if isinstance(item, dict)],
                warnings=[str(item) for item in result.get("warnings", [])],
            )
        except Exception:
            return EvidenceSearchResult(status="error", warnings=["OEM evidence retrieval failed"])
