from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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
