from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CaseResolutionMethod = Literal["EXACT_INTERNAL_ID", "UNRESOLVED"]
CaseFactsStatus = Literal[
    "FOUND",
    "CASE_NOT_FOUND",
    "CASE_FOUND_NO_DOCUMENTS",
    "CASE_FOUND_NO_CHUNKS",
    "AMBIGUOUS_ID_MAPPING",
    "RETRIEVAL_UNAVAILABLE",
]
FactCategory = Literal["problem", "activity", "calculation", "document"]
ReferenceUsage = Literal["DIRECTLY_CITED", "LISTED_ONLY"]
ReferenceRole = Literal[
    "TECHNICAL_BASIS",
    "ANALYSIS_BASIS",
    "HISTORICAL_DELIVERABLE",
    "PROCESS_REFERENCE",
    "CONTEXT_ONLY",
    "UNKNOWN",
]


class CaseFactsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=100)
    categories: list[FactCategory] = Field(
        default_factory=lambda: ["problem", "activity", "calculation", "document"],
        min_length=1,
    )
    max_evidence_per_category: int = Field(default=5, ge=1, le=20)
    include_references: bool = True


class CaseResolution(BaseModel):
    requested_case_id: str
    resolved_case_id: str | None = None
    resolution_method: CaseResolutionMethod
    resolution_evidence: str
    candidate_case_ids: list[str] = Field(default_factory=list)


class CorpusSummary(BaseModel):
    case_found: bool = False
    document_count: int = 0
    chunk_count: int = 0
    reference_count: int = 0


class HistoricalReference(BaseModel):
    ref_id: str
    document_number: str = ""
    title: str = ""
    document_type: str = ""
    raw_text: str = ""
    usage: ReferenceUsage
    role: ReferenceRole
    source_document_id: str = ""
    source_table_id: str = ""


class HistoricalFact(BaseModel):
    category: FactCategory
    value: str
    document_id: str
    chunk_id: str
    evidence_text: str
    references: list[HistoricalReference] = Field(default_factory=list)


class CaseFactsResponse(BaseModel):
    status: CaseFactsStatus
    requested_case_id: str
    resolved_case_id: str | None = None
    resolution_method: CaseResolutionMethod
    resolution_evidence: str
    candidate_case_ids: list[str] = Field(default_factory=list)
    corpus: CorpusSummary = Field(default_factory=CorpusSummary)
    facts: list[HistoricalFact] = Field(default_factory=list)
    listed_references: list[HistoricalReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class CaseSummary:
    case_id: str
    aircraft_type: str = ""
    msn: str = ""
    subject: str = ""
    problem_summary: str = ""
    ata_list: list[str] = field(default_factory=list)
    applicable_ap_refs: list[str] = field(default_factory=list)
    source_document_id: str = ""
    source_system: str = "mro_rag"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentChunk:
    case_id: str
    document_id: str
    chunk_id: str
    chunk_kind: str
    section_title: str
    text: str
    search_text: str = ""
    section_label: str = ""
    heading_path: list[str] = field(default_factory=list)
    chunk_level: str = ""
    document_family: str = ""
    table_refs: list[str] = field(default_factory=list)
    citation_refs: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    page_number: int | None = None
    source_system: str = "mro_rag"
    source_file: str = ""
    block_id: str = ""
    vault_note_path: str = ""
    page_image_path: str = ""


@dataclass(slots=True)
class DocumentRecord:
    """Controlled-document registry fields used by ATA evidence retrieval.

    The registry is deliberately separate from a chunk's free-form metadata: an
    ATA conclusion must be auditable without relying on a filename or an LLM
    interpretation of a historical case.
    """

    document_id: str
    document_type: str = ""
    issuer: str = ""
    aircraft_type: str = ""
    effectivity: str = ""
    ata: str = ""
    revision: str = ""
    issue_date: str = ""
    section_reference: str = ""
    source_url: str = ""
    source_path: str = ""
    trust_level: str = "internal_reference"
    source_origin: str = "internal"


@dataclass(slots=True)
class DocumentReference:
    case_id: str
    document_id: str
    ref_id: str
    marker: str = ""
    title: str = ""
    document_number: str = ""
    document_type: str = ""
    raw_text: str = ""
    source_document_id: str = ""
    source_table_id: str = ""
    source_file: str = ""
    raw_json: dict[str, object] = field(default_factory=dict)
