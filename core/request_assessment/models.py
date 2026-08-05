from __future__ import annotations

from enum import Enum
from typing import Any

from ._pydantic import BaseModel, Field


class AssessmentDecision(str, Enum):
    ACCEPT_FOR_QUOTATION = "ACCEPT_FOR_QUOTATION"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    EXPERT_REVIEW = "EXPERT_REVIEW"
    DECLINE = "DECLINE"


class AssessmentStatus(str, Enum):
    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    WAITING_FOR_INFORMATION = "WAITING_FOR_INFORMATION"
    WAITING_FOR_EXPERT = "WAITING_FOR_EXPERT"
    DECISION_READY = "DECISION_READY"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class MissingImportance(str, Enum):
    BLOCKING = "BLOCKING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NON_BLOCKING = "NON_BLOCKING"


class AssessmentResultStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    REVIEW = "REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CapabilityRegistryMode(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    ADVISORY = "ADVISORY"
    CONTROLLED = "CONTROLLED"


class DocumentaryAssessmentStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceRelevance(str, Enum):
    DIRECT_MATCH = "DIRECT_MATCH"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    NOT_RELEVANT = "NOT_RELEVANT"


class ApplicabilityStatus(str, Enum):
    APPLICABLE = "APPLICABLE"
    POTENTIALLY_APPLICABLE = "POTENTIALLY_APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class SourceInput(BaseModel):
    request_text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class BusinessContext(BaseModel):
    customer: str | None = None
    requested_due_date: str | None = None
    requested_deliverables: list[str] = Field(default_factory=list)
    approval_expectation: str | None = None
    jurisdiction: str | None = None


class ConfirmedInputs(BaseModel):
    aircraft_type: str | None = None
    aircraft_model: str | None = None
    msn: str | None = None
    registration: str | None = None
    part_number: str | None = None
    zone: str | None = None


class MissingInformation(BaseModel):
    code: str
    field: str
    category: str
    importance: MissingImportance = MissingImportance.REVIEW_REQUIRED
    blocking_stage: str = "ASSESSMENT"
    target: str = "CUSTOMER"
    reason: str
    source: str


class ClarificationQuestion(BaseModel):
    question_id: str
    field: str
    target: str = "CUSTOMER"
    question: str
    answer_type: str = "text"
    required: bool = True
    reason: str
    requested_attachments: list[str] = Field(default_factory=list)


class ClarificationAnswer(BaseModel):
    question_id: str
    answer: Any
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class SelectedSimilarCase(BaseModel):
    case_id: str
    group: str
    similarity_class: str = ""
    confidence: str | float | None = None
    scores: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(BaseModel):
    case_id: str | None = None
    internal_case_id: str | None = None
    document_id: str | None = None
    source_document_id: str | None = None
    chunk_id: str | None = None
    parent_id: str | None = None
    section_title: str | None = None
    citation_refs: list[str] = Field(default_factory=list)
    retrieval_mode: str | None = None
    rerank_score: float | None = None
    final_score: float | None = None
    document_type: str | None = None
    revision: str | None = None
    effectivity: str | None = None
    aircraft_type: str | None = None
    aircraft_model: str | None = None
    msn: str | None = None
    ata: list[str] = Field(default_factory=list)
    object: str | None = None
    damage_type: str | None = None
    snippet: str = ""
    relevance: dict[str, Any] = Field(default_factory=dict)
    applicability: dict[str, Any] = Field(default_factory=dict)
    source_descriptor: dict[str, Any] = Field(default_factory=dict)


class CapabilityContext(BaseModel):
    aircraft_family: str | None = None
    aircraft_model: str | None = None
    product_scope: str | None = None
    work_type: str | None = None
    affected_ata: list[str] = Field(default_factory=list)
    potentially_affected_ata: list[str] = Field(default_factory=list)
    disciplines: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    approval_expectation: str | None = None
    jurisdiction: str | None = None
    physical_objects: list[str] = Field(default_factory=list)
    damage_types: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class ApprovalRouteAssessment(BaseModel):
    route: str
    status: AssessmentResultStatus
    jurisdictions: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    source_document: str | None = None
    source_revision: str | None = None
    limitations: list[str] = Field(default_factory=list)


class CapabilityAssessment(BaseModel):
    status: AssessmentResultStatus
    matched_capability_ids: list[str] = Field(default_factory=list)
    hard_fail_reasons: list[str] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_documents: list[str] = Field(default_factory=list)
    registry_version: str | None = None
    registry_mode: CapabilityRegistryMode = CapabilityRegistryMode.UNAVAILABLE
    verification_status: str | None = None
    dimension_results: dict[str, AssessmentResultStatus] = Field(default_factory=dict)
    matched_approval_routes: list[ApprovalRouteAssessment] = Field(default_factory=list)


class ApprovalAssessment(BaseModel):
    status: AssessmentResultStatus
    possible_routes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    matched_routes: list[ApprovalRouteAssessment] = Field(default_factory=list)


class DocumentaryAssessment(BaseModel):
    status: DocumentaryAssessmentStatus
    verification_required: bool
    requested_case_ids: list[str] = Field(default_factory=list)
    confirmed_case_ids: list[str] = Field(default_factory=list)
    usable_evidence_ids: list[str] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DecisionReason(BaseModel):
    code: str
    source: str
    message: str


class DecisionResult(BaseModel):
    status: AssessmentDecision
    reasons: list[DecisionReason] = Field(default_factory=list)
    blocking_items: list[str] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    requires_human_approval: bool = True


class HumanReview(BaseModel):
    action: str
    final_decision: AssessmentDecision | None = None
    comment: str = ""


class ExternalCallTrace(BaseModel):
    service: str
    method: str = "POST"
    safe_url: str
    status: str
    http_status: int | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    warning: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AssessmentState(BaseModel):
    request_id: str
    status: AssessmentStatus = AssessmentStatus.RECEIVED
    source: SourceInput = Field(default_factory=SourceInput)
    business_context: BusinessContext = Field(default_factory=BusinessContext)
    confirmed_inputs: ConfirmedInputs = Field(default_factory=ConfirmedInputs)
    confirmed_additional_data: dict[str, Any] = Field(default_factory=dict)
    ata_impact: dict[str, Any] | None = None
    similar_cases: dict[str, Any] | None = None
    missing_information: list[MissingInformation] = Field(default_factory=list)
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    answers: list[ClarificationAnswer] = Field(default_factory=list)
    selected_similar_cases: list[SelectedSimilarCase] = Field(default_factory=list)
    mro_kb_evidence: list[EvidenceRecord] = Field(default_factory=list)
    documentary_assessment: DocumentaryAssessment | None = None
    capability_assessment: CapabilityAssessment | None = None
    approval_assessment: ApprovalAssessment | None = None
    decision: DecisionResult | None = None
    human_review: HumanReview | None = None
    external_call_trace: list[ExternalCallTrace] = Field(default_factory=list)
    workflow_iteration: int = 1
    warnings: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
