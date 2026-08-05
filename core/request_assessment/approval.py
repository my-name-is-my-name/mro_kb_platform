from __future__ import annotations

from .models import ApprovalAssessment, AssessmentResultStatus, CapabilityAssessment, CapabilityContext, CapabilityRegistryMode


def assess_approval(context: CapabilityContext, capability: CapabilityAssessment | None) -> ApprovalAssessment:
    if capability and capability.status == AssessmentResultStatus.FAIL:
        return ApprovalAssessment(status=AssessmentResultStatus.FAIL, blocking_reasons=list(capability.hard_fail_reasons))
    if not capability:
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, review_items=["Approval registry unavailable"])
    if capability.registry_mode != CapabilityRegistryMode.CONTROLLED:
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, review_items=["Approval route requires controlled registry confirmation"])
    routes = capability.matched_approval_routes
    failing = [route for route in routes if route.status == AssessmentResultStatus.FAIL]
    if failing:
        return ApprovalAssessment(status=AssessmentResultStatus.FAIL, blocking_reasons=["Controlled approval route prohibition matched"], matched_routes=failing)
    if not routes:
        return ApprovalAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["No controlled approval route found"])
    matching = []
    for route in routes:
        jurisdiction_ok = not context.jurisdiction or not route.jurisdictions or context.jurisdiction.upper() in {item.upper() for item in route.jurisdictions}
        deliverables_ok = not context.deliverables or not route.deliverables or all(item.lower() in {value.lower() for value in route.deliverables} for item in context.deliverables)
        source_ok = bool(route.source_document and route.source_revision)
        if jurisdiction_ok and deliverables_ok and source_ok and route.status == AssessmentResultStatus.PASS:
            matching.append(route)
    if matching:
        return ApprovalAssessment(status=AssessmentResultStatus.PASS, possible_routes=[route.route for route in matching], matched_routes=matching)
    if context.jurisdiction:
        return ApprovalAssessment(status=AssessmentResultStatus.REVIEW, review_items=["Confirm jurisdiction-specific approval route"])
    return ApprovalAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["Approval route not confirmed"])
