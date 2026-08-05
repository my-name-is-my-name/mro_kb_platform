from __future__ import annotations

from .models import AssessmentState, HistoricalFact


def build_final_content(state: AssessmentState) -> str:
    decision = state.decision.status.value if state.decision else "EXPERT_REVIEW"
    inference = state.historical_inference
    lines = [
        f"Предварительная рекомендация: {decision}.",
        "",
        "Готовность к оценке трудоёмкости:",
        f"{state.quotation_readiness}.",
        "",
        "Историческая основа:",
    ]
    if not inference or inference.historical_support in {"NONE", "UNAVAILABLE"}:
        if inference and inference.historical_support == "UNAVAILABLE":
            lines.append("- MRO KB недоступен; отсутствие документов не выводится.")
        else:
            lines.append("- Прямые исторические аналоги не найдены или документы не подтвердили прошлый work package.")
        lines.extend([
            "",
            "Конкретный состав расчётов и документов историческими данными не подтверждён.",
            "",
            "Рекомендуемые следующие шаги:",
            "- уточнить исходные данные;",
            "- выполнить экспертное определение scope;",
            "- проверить применимую документацию.",
        ])
    else:
        if inference.historical_support == "DIRECT":
            lines.append(f"- найден прямой или сильный аналог: {', '.join(inference.selected_case_ids) or 'не указан'};")
        else:
            lines.append(f"- найдены частичные исторические материалы: {', '.join(inference.selected_case_ids) or 'wide search'};")
        evidence_count = len({eid for fact in inference.facts for eid in fact.evidence_ids})
        if evidence_count:
            lines.append(f"- найдены подтверждающие evidence records: {evidence_count}.")
        _section(lines, "По историческим заявкам выполнялись:", _values(inference.facts, {"activity", "calculation"}))
        _section(lines, "Выпускались:", _values(inference.facts, {"document"}))
        _section(lines, "Предлагаемый предварительный scope:", _format_scope(inference.proposed_scope))
    if inference:
        _section(lines, "Отличия новой заявки:", inference.differences or ["существенные отличия не выделены по доступным структурированным полям."])
        _section(lines, "Допущения:", inference.assumptions or ["Предположения не используются как исторические факты."])
        _section(lines, "Для подтверждения требуется:", inference.missing_inputs or ["формальная capability и approval route проверка."])
        if inference.warnings:
            _section(lines, "Warnings:", inference.warnings[:8])
    lines.extend([
        "",
        "Формальная capability и approval route требуют отдельного подтверждения.",
    ])
    return "\n".join(lines).strip()


def _section(lines: list[str], title: str, values: list[str]) -> None:
    if not values:
        return
    lines.extend(["", title])
    lines.extend(f"- {value}" for value in values)


def _values(facts: list[HistoricalFact], categories: set[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.category not in categories:
            continue
        key = fact.value.lower()
        if key not in seen:
            seen.add(key)
            result.append(fact.raw_value or fact.value)
    return result


def _format_scope(facts: list[HistoricalFact]) -> list[str]:
    result = []
    for fact in facts:
        sources = ", ".join(fact.source_case_ids) if fact.source_case_ids else fact.source_case_id or "customer request"
        evidence = f"; evidence: {', '.join(fact.evidence_ids)}" if fact.evidence_ids else ""
        result.append(f"{fact.category}: {fact.raw_value or fact.value} ({fact.basis}; source: {sources}{evidence})")
    return result
