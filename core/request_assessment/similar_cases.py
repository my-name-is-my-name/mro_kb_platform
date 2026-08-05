from __future__ import annotations

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
    selected: list[SelectedSimilarCase] = []
    selected.extend(_select_group(payload.get("accepted"), "accepted", 3))
    selected.extend(_select_group(payload.get("not_accepted"), "not_accepted", 2))
    selected.extend(_select_group(payload.get("intermediate"), "intermediate", 1))
    stronger = [item for item in selected if item.similarity_class != "weak_analog"]
    return stronger or selected


def qualified_cases(payload: dict[str, Any] | None) -> bool:
    return bool(select_similar_cases(payload))


def _select_group(value: Any, group: str, limit: int) -> list[SelectedSimilarCase]:
    cases = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    cases.sort(key=_rank, reverse=True)
    return [
        SelectedSimilarCase(
            case_id=str(item.get("case_id") or item.get("id") or ""),
            group=group,
            similarity_class=str(item.get("similarity_reason_class") or item.get("similarity_class") or ""),
            confidence=item.get("similarity_confidence"),
            scores={key: item.get(key) for key in ("structured_score", "semantic_score", "rerank_score") if key in item},
            reasons=[str(reason) for reason in item.get("reasons", [])] if isinstance(item.get("reasons"), list) else [],
            source=item,
        )
        for item in cases[:limit]
        if str(item.get("case_id") or item.get("id") or "").strip()
    ]


def _rank(item: dict[str, Any]) -> tuple[int, float, float, float]:
    klass = str(item.get("similarity_reason_class") or item.get("similarity_class") or "")
    return (
        PRIORITY.get(klass, 0),
        _confidence(item.get("similarity_confidence")),
        _number(item.get("structured_score")),
        max(_number(item.get("rerank_score")), _number(item.get("semantic_score"))),
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
