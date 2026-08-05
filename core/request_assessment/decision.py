from __future__ import annotations

from .models import (
    ApprovalAssessment,
    AssessmentDecision,
    AssessmentResultStatus,
    CapabilityAssessment,
    CapabilityRegistryMode,
    DecisionReason,
    DecisionResult,
    DocumentaryAssessment,
    DocumentaryAssessmentStatus,
    MissingInformation,
)


def make_decision(
    missing: list[MissingInformation],
    capability: CapabilityAssessment | None,
    approval: ApprovalAssessment | None,
    documentary: DocumentaryAssessment | None,
    registry_mode: CapabilityRegistryMode,
    extra_review_reasons: list[DecisionReason] | None = None,
) -> DecisionResult:
    if registry_mode == CapabilityRegistryMode.CONTROLLED and capability and capability.status == AssessmentResultStatus.FAIL:
        return DecisionResult(
            status=AssessmentDecision.DECLINE,
            reasons=[DecisionReason(code="CAPABILITY_HARD_FAIL", source="capability_assessment", message=reason) for reason in capability.hard_fail_reasons],
            blocking_items=list(capability.hard_fail_reasons),
        )
    if registry_mode == CapabilityRegistryMode.CONTROLLED and approval and approval.status == AssessmentResultStatus.FAIL:
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
    if registry_mode != CapabilityRegistryMode.CONTROLLED:
        reasons.append(DecisionReason(code="CAPABILITY_REGISTRY_NOT_CONTROLLED", source="capability_assessment", message="Controlled capability registry is required for automatic ACCEPT or DECLINE."))
        review_items.append(f"Capability registry mode is {registry_mode.value}")
    if not capability or capability.status in {AssessmentResultStatus.UNKNOWN, AssessmentResultStatus.REVIEW}:
        review_items.extend(capability.review_items if capability else ["Capability registry unavailable"])
        reasons.append(DecisionReason(code="CAPABILITY_REQUIRES_REVIEW", source="capability_assessment", message="Capability is not fully confirmed."))
    if not approval or approval.status in {AssessmentResultStatus.UNKNOWN, AssessmentResultStatus.REVIEW}:
        review_items.extend(approval.review_items if approval else ["Approval route unavailable"])
        reasons.append(DecisionReason(code="APPROVAL_ROUTE_REQUIRES_CONFIRMATION", source="approval_assessment", message="Approval route requires confirmation."))
    if documentary and documentary.verification_required and documentary.status in {
        DocumentaryAssessmentStatus.UNAVAILABLE,
        DocumentaryAssessmentStatus.INCONCLUSIVE,
        DocumentaryAssessmentStatus.NOT_CONFIRMED,
        DocumentaryAssessmentStatus.PARTIALLY_CONFIRMED,
    }:
        review_items.extend(documentary.review_items or ["Documentary verification is not confirmed"])
        reasons.append(DecisionReason(code=f"DOCUMENTARY_{documentary.status.value}", source="mro_kb", message="Documentary evidence is not sufficient for automatic quotation gate."))
    if reasons or review_items:
        return DecisionResult(status=AssessmentDecision.EXPERT_REVIEW, reasons=reasons, review_items=review_items)
    return DecisionResult(
        status=AssessmentDecision.ACCEPT_FOR_QUOTATION,
        reasons=[DecisionReason(code="ALL_CONTROLLED_CHECKS_PASS", source="decision_engine", message="All mandatory controlled deterministic checks passed.")],
    )

