from __future__ import annotations

from .ata_context import normalize_ata_context
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
    if inference and inference.historical_support == "CANDIDATES_ONLY":
        count = _candidate_count(state) or len(inference.selected_case_ids)
        lines.extend(["", "Предварительные исторические кандидаты:"])
        lines.extend(
            [
                f"- найдено {count} потенциально похожих исторических заявок;",
                "- документальная проверка этих заявок через MRO KB не выполнялась по текущей политике маршрутизации;",
                "- найденные заявки являются кандидатами и пока не подтверждают применимость исторического work package.",
            ]
        )
        _section(lines, "Предварительный технический контекст:", _technical_context(state))
        _section(lines, "Для продолжения необходимо:", _continuation_items(inference.missing_inputs))
        lines.extend(
            [
                "",
                "После уточнения данных сервис проверит исторические заявки через MRO KB и сформирует предварительный состав работ.",
            ]
        )
    elif not inference or inference.historical_support in {"NONE", "UNAVAILABLE"}:
        if inference and inference.historical_support == "UNAVAILABLE":
            lines.append("- MRO KB недоступен; отсутствие документов не выводится.")
        elif _mro_kb_was_called(state):
            lines.append("- MRO KB был проверен, но документы не подтвердили применимый historical work package.")
        else:
            lines.append("- Прямые исторические аналоги не найдены.")
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
        if inference.historical_support != "CANDIDATES_ONLY":
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


def _technical_context(state: AssessmentState) -> list[str]:
    ctx = normalize_ata_context(state.ata_impact, _fields(state), state.business_context.requested_deliverables, state.source.request_text)
    result: list[str] = []
    objects = ctx.structural_elements or ctx.physical_objects
    if objects:
        result.append(f"объект: {', '.join(objects[:3])}")
    if ctx.damage_types:
        result.append(f"повреждение: {', '.join(ctx.damage_types[:3])}")
    ata = ctx.affected_ata or ctx.context_ata or ctx.potentially_affected_ata
    if ata:
        result.append(f"предварительная ATA: {', '.join(_ata_numbers(ata[:3]))}")
    return result


def _continuation_items(missing_inputs: list[str]) -> list[str]:
    items: list[str] = []
    missing_text = " ".join(missing_inputs).lower()
    if "aircraft" in missing_text or "тип вс" in missing_text or "aircraft_type" in missing_text or "aircraft_model" in missing_text:
        items.append("указать тип или модель ВС, если они не были указаны;")
    else:
        items.append("указать тип или модель ВС, если они не были указаны;")
    if "damage" in missing_text or "трещ" in missing_text or "размер" in missing_text or "location" in missing_text:
        items.append("при необходимости уточнить размеры или расположение трещины.")
    else:
        items.append("при необходимости уточнить размеры или расположение трещины.")
    return items


def _fields(state: AssessmentState) -> dict[str, object]:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    fields.update(state.confirmed_additional_data)
    return fields


def _ata_numbers(values: list[str]) -> list[str]:
    result = []
    for value in values:
        text = str(value).strip()
        result.append(text.replace("ATA ", "") if text.upper().startswith("ATA ") else text)
    return result


def _candidate_count(state: AssessmentState) -> int:
    payload = state.similar_cases if isinstance(state.similar_cases, dict) else {}
    total = 0
    for group in ("accepted", "not_accepted", "intermediate"):
        items = payload.get(group)
        if isinstance(items, list):
            total += len([item for item in items if isinstance(item, dict)])
    return total


def _mro_kb_was_called(state: AssessmentState) -> bool:
    documentary = state.documentary_assessment
    return bool(documentary and documentary.verification_required)
