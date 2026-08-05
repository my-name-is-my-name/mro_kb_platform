from __future__ import annotations

import json

from .models import AssessmentState


def build_final_content(state: AssessmentState) -> str:
    decision = state.decision.status.value if state.decision else "EXPERT_REVIEW"
    payload = state.model_dump(mode="json")
    return (
        f"Предварительная рекомендация: {decision}\n\n"
        "Итоговый assessment:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

