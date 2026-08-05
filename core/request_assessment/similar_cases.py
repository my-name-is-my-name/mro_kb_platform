from __future__ import annotations

import os
from typing import Any

from .models import SelectedSimilarCase

PRIORITY = {
    "same_identifier": 7,
    "same_component_defect_zone": 6,
    "same_component_defect": 5,
    "same_work_type": 4,
    "commercially_similar": 3,
    "strong_lexical_analog": 2,
    "weak_analog": 1,
}


def has_embedded_similar_cases(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").lower() in {"disabled", "unavailable"}:
        return False
    if payload.get("status") == "ok":
        return True
    if payload.get("similarity_status"):
        return True
    return False


def is_similar_cases_unavailable(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return True
    return str(payload.get("status") or "").lower() in {"", "disabled", "unavailable"}


def select_similar_cases(payload: dict[str, Any] | None) -> list[SelectedSimilarCase]:
    if not isinstance(payload, dict):
        return []
    limit = _max_rag_cases()
    candidates: list[tuple[tuple[int, float, float, float, int], SelectedSimilarCase]] = []
    for group in ("accepted", "not_accepted", "intermediate"):
        cases = [item for item in payload.get(group) if isinstance(item, dict)] if isinstance(payload.get(group), list) else []
        for item in cases:
            case_id = str(item.get("case_id") or item.get("id") or "").strip()
            if not case_id:
                continue
            selected = SelectedSimilarCase(
                case_id=case_id,
                group=group,
                similarity_class=str(item.get("similarity_reason_class") or item.get("similarity_class") or ""),
                confidence=item.get("similarity_confidence"),
                scores={key: item.get(key) for key in ("structured_score", "semantic_score", "rerank_score") if key in item},
                reasons=[str(reason) for reason in item.get("reasons", [])] if isinstance(item.get("reasons"), list) else [],
                source=item,
            )
            candidates.append((_rank(item), selected))
    candidates.sort(key=lambda value: value[0], reverse=True)
    selected = [item for _, item in candidates]
    stronger = [item for item in selected if item.similarity_class != "weak_analog"]
    return (stronger or selected)[:limit]


def qualified_cases(payload: dict[str, Any] | None) -> bool:
    return bool(select_similar_cases(payload))


def _rank(item: dict[str, Any]) -> tuple[int, float, float, float, int]:
    klass = str(item.get("similarity_reason_class") or item.get("similarity_class") or "")
    warnings = item.get("warnings")
    warning_penalty = len(warnings) if isinstance(warnings, list) else 0
    return (
        PRIORITY.get(klass, 0),
        _confidence(item.get("similarity_confidence")),
        _number(item.get("structured_score")),
        max(_number(item.get("rerank_score")), _number(item.get("semantic_score"))),
        -warning_penalty,
    )


def _confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return {"high": 3.0, "medium": 2.0, "low": 1.0}.get(str(value).lower(), 0.0)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _max_rag_cases() -> int:
    try:
        return max(1, int(os.environ.get("MRO_ASSESSMENT_MAX_RAG_CASES", "3")))
    except ValueError:
        return 3
