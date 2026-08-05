from __future__ import annotations

from .models import (
    ApprovalAssessment,
    AssessmentDecision,
    AssessmentResultStatus,
    CapabilityAssessment,
    DecisionReason,
    DecisionResult,
    MissingInformation,
)


def make_decision(
    missing: list[MissingInformation],
    capability: CapabilityAssessment | None,
    approval: ApprovalAssessment | None,
    document_review_required: bool = False,
    extra_review_reasons: list[DecisionReason] | None = None,
) -> DecisionResult:
    if capability and capability.status == AssessmentResultStatus.FAIL:
        return DecisionResult(
            status=AssessmentDecision.DECLINE,
            reasons=[DecisionReason(code="CAPABILITY_HARD_FAIL", source="capability_assessment", message=reason) for reason in capability.hard_fail_reasons],
            blocking_items=list(capability.hard_fail_reasons),
        )
    if approval and approval.status == AssessmentResultStatus.FAIL:
        return DecisionResult(
            status=AssessmentDecision.DECLINE,
            reasons=[DecisionReason(code="APPROVAL_HARD_FAIL", source="approval_assessment", message=reason) for reason in approval.blocking_reasons],
            blocking_items=list(approval.blocking_reasons),
        )
    blocking = [item for item in missing if item.importance.value == "BLOCKING"]
    if blocking:
        return DecisionResult(
            status=AssessmentDecision.REQUEST_INFORMATION,
            reasons=[DecisionReason(code=item.code, source=item.source, message=item.reason) for item in blocking],
            blocking_items=[item.field for item in blocking],
        )
    reasons = list(extra_review_reasons or [])
    review_items: list[str] = []
    if not capability or capability.status in {AssessmentResultStatus.UNKNOWN, AssessmentResultStatus.REVIEW}:
        review_items.extend((capability.review_items if capability else ["Capability registry unavailable"]))
        reasons.append(DecisionReason(code="CAPABILITY_REQUIRES_REVIEW", source="capability_assessment", message="Capability is not fully confirmed."))
    if not approval or approval.status in {AssessmentResultStatus.UNKNOWN, AssessmentResultStatus.REVIEW}:
        review_items.extend((approval.review_items if approval else ["Approval route unavailable"]))
        reasons.append(DecisionReason(code="APPROVAL_ROUTE_REQUIRES_CONFIRMATION", source="approval_assessment", message="Approval route requires confirmation."))
    if document_review_required:
        review_items.append("Documental verification unavailable or inconclusive")
        reasons.append(DecisionReason(code="DOCUMENTAL_VERIFICATION_REQUIRES_REVIEW", source="mro_kb", message="Document evidence is not sufficient for automatic quotation gate."))
    if reasons or review_items:
        return DecisionResult(status=AssessmentDecision.EXPERT_REVIEW, reasons=reasons, review_items=review_items)
    return DecisionResult(status=AssessmentDecision.ACCEPT_FOR_QUOTATION, reasons=[DecisionReason(code="ALL_REQUIRED_CHECKS_PASS", source="decision_engine", message="All mandatory deterministic checks passed.")])

