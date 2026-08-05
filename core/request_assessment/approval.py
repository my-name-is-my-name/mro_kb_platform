from __future__ import annotations

from .models import ApprovalAssessment, AssessmentResultStatus, CapabilityAssessment, CapabilityContext


def assess_approval(context: CapabilityContext, capability: CapabilityAssessment | None) -> ApprovalAssessment:
    if capability and capability.status == AssessmentResultStatus.FAIL:
        return ApprovalAssessment(status=AssessmentResultStatus.FAIL, blocking_reasons=list(capability.hard_fail_reasons))
    if not capability or capability.status == AssessmentResultStatus.UNKNOWN:
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, review_items=["Confirm approval route after capability review"])
    needs_approved = any("approved" in item.lower() or "stress" in item.lower() or "repair" in item.lower() for item in context.deliverables)
    routes = ["INTERNAL_DOA", "EXTERNAL_DOA"] if needs_approved else ["INTERNAL_ENGINEERING_REVIEW"]
    if context.jurisdiction and context.jurisdiction.upper() not in {"EASA", "LOCAL", "RU", "UAE"}:
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, possible_routes=routes, review_items=["Confirm jurisdiction-specific approval route"])
    if needs_approved and context.approval_expectation and "OEM" in context.approval_expectation.upper():
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, possible_routes=["OEM", "EXTERNAL_DOA"], review_items=["Confirm external approval availability"])
    return ApprovalAssessment(status=AssessmentResultStatus.PASS, possible_routes=routes)

