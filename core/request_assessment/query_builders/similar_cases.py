from __future__ import annotations

from typing import Any

from core.request_assessment.ata_context import normalize_ata_context
from core.request_assessment.models import AssessmentState


def build_similar_cases_payload(state: AssessmentState) -> dict[str, Any]:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    fields.update(state.confirmed_additional_data)
    ctx = normalize_ata_context(state.ata_impact, fields, state.business_context.requested_deliverables, state.source.request_text)
    return {
        "request_id": state.request_id,
        "query": _compact_query(state, ctx),
        "context": {
            "aircraft_type": ctx.aircraft_model,
            "ata": ctx.affected_ata,
            "components": ctx.physical_objects + ctx.structural_elements,
            "zones": ctx.locations,
            "identifiers": _non_msn_identifiers(ctx.identifiers),
            "defect_type": ", ".join(ctx.damage_types + ctx.damage_descriptions),
            "work_type": (ctx.work_type or "UNKNOWN").lower().replace("_", " "),
            "action_required": ctx.maintenance_action,
            "requested_deliverables": [item.replace("_", " ") for item in state.business_context.requested_deliverables],
        },
        "limits": {"accepted": 5, "not_accepted": 5, "intermediate": 3},
        "retrieval_mode": "legacy_ranked_query",
    }


def _compact_query(state: AssessmentState, ctx: object) -> str:
    pieces = [
        state.source.request_text,
        getattr(ctx, "aircraft_model", None),
        ", ".join(getattr(ctx, "affected_ata", [])),
        ", ".join(getattr(ctx, "physical_objects", []) + getattr(ctx, "structural_elements", [])),
        ", ".join(getattr(ctx, "damage_types", []) + getattr(ctx, "damage_descriptions", [])),
        ", ".join(state.business_context.requested_deliverables),
    ]
    return " | ".join(str(item) for item in pieces if str(item or "").strip())


def _non_msn_identifiers(values: list[str]) -> list[str]:
    return [value for value in values if "msn" not in value.lower() and not value.strip().isdigit()]

