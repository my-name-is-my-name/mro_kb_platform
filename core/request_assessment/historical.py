from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .ata_context import NormalizedAtaContext, normalize_ata_context
from .models import AssessmentState, EvidenceRecord, HistoricalFact, HistoricalInference, SelectedSimilarCase

FACT_CATEGORIES = {
    "activity",
    "calculation",
    "document",
    "customer_input",
    "discipline",
    "reference",
    "constraint",
    "outcome",
}

CATEGORY_ALIASES = {
    "activities": "activity",
    "activity": "activity",
    "engineering_work": "activity",
    "work": "activity",
    "analyses_or_calculations": "calculation",
    "analysis": "calculation",
    "analyses": "calculation",
    "calculation": "calculation",
    "calculations": "calculation",
    "documents": "document",
    "document": "document",
    "deliverables": "document",
    "customer_inputs": "customer_input",
    "customer_input": "customer_input",
    "requested_inputs": "customer_input",
    "input_data": "customer_input",
    "disciplines": "discipline",
    "discipline": "discipline",
    "references": "reference",
    "reference": "reference",
    "constraints": "constraint",
    "constraint": "constraint",
    "limitations": "constraint",
    "risks": "constraint",
    "outcome": "outcome",
    "result": "outcome",
}

SAFE_SYNONYMS = {
    "stress substantiation": "static strength substantiation",
    "strength substantiation": "static strength substantiation",
    "repair dwg": "repair drawing",
}


def extract_historical_facts(
    case_id: str | None,
    payload: dict[str, Any],
    evidence: list[EvidenceRecord],
) -> tuple[list[HistoricalFact], list[str]]:
    warnings: list[str] = []
    facts: list[HistoricalFact] = []
    facts.extend(_facts_from_mapping(case_id, payload))

    answer = str(payload.get("answer") or "").strip()
    if answer:
        parsed, json_warning = _json_from_answer(answer)
        if json_warning:
            warnings.append(json_warning)
        if isinstance(parsed, dict):
            facts.extend(_facts_from_mapping(case_id, parsed))
        elif parsed is None:
            warnings.append(f"unstructured_answer_saved:{answer[:500]}")

    facts.extend(_facts_from_evidence_metadata(case_id, payload, evidence))
    facts = _dedupe_facts(facts)
    if answer and not facts:
        warnings.append("mro_kb_answer_without_structured_facts")
    if not facts and _raw_sources_count(payload):
        warnings.append("mro_kb_sources_without_extractable_facts")
    return facts, warnings


def build_historical_inference(
    state: AssessmentState,
    facts: list[HistoricalFact],
    warnings: list[str] | None = None,
    unavailable: bool = False,
) -> HistoricalInference:
    selected = list(state.selected_similar_cases)
    ctx = normalize_ata_context(state.ata_impact, _fields(state), state.business_context.requested_deliverables, state.source.request_text)
    inference = HistoricalInference(
        selected_case_ids=[case.case_id for case in selected],
        facts=_dedupe_facts(facts),
        warnings=list(warnings or []),
    )
    if unavailable:
        inference.historical_support = "UNAVAILABLE"
    elif selected and not inference.facts and "HISTORICAL_CANDIDATES_NOT_VERIFIED" in inference.warnings:
        inference.historical_support = "CANDIDATES_ONLY"
    elif _has_direct_support(selected, inference.facts):
        inference.historical_support = "DIRECT"
    elif inference.facts:
        inference.historical_support = "PARTIAL"
    else:
        inference.historical_support = "NONE"
        if not selected:
            inference.warnings.append("direct_historical_analogs_not_found")

    inference.proposed_scope = _proposed_scope(state, selected, inference.facts)
    inference.differences = _differences(selected, ctx)
    inference.assumptions = _assumptions(inference, selected)
    inference.missing_inputs = _missing_inputs(state, ctx)
    return inference


def quotation_readiness(state: AssessmentState, unavailable: bool = False) -> str:
    if state.missing_information:
        blocking = [item for item in state.missing_information if item.importance.value == "BLOCKING"]
        if blocking:
            return "NEEDS_INFORMATION"
    inference = state.historical_inference
    if unavailable or (inference and inference.historical_support == "UNAVAILABLE"):
        return "NEEDS_EXPERT_REVIEW"
    if inference and inference.historical_support == "DIRECT" and inference.proposed_scope:
        return "READY_FOR_ESTIMATION"
    return "NEEDS_EXPERT_REVIEW"


def _facts_from_mapping(case_id: str | None, payload: dict[str, Any]) -> list[HistoricalFact]:
    result: list[HistoricalFact] = []
    for key in ("facts", "historical_facts"):
        values = payload.get(key)
        if isinstance(values, list):
            for item in values:
                fact = _fact_from_item(case_id, item, None)
                if fact:
                    result.append(fact)
    for raw_category, category in CATEGORY_ALIASES.items():
        values = payload.get(raw_category)
        if values is None:
            continue
        for item in _as_items(values):
            fact = _fact_from_item(case_id, item, category)
            if fact:
                result.append(fact)
    return result


def _fact_from_item(case_id: str | None, item: Any, default_category: str | None) -> HistoricalFact | None:
    if isinstance(item, dict):
        category = _category(item.get("category") or item.get("type") or default_category)
        value = _first(item.get("value"), item.get("text"), item.get("name"), item.get("title"), item.get("description"))
        if not category or not value:
            return None
        evidence_ids = _evidence_ids_from_item(item)
        source_case_id = _first(item.get("source_case_id"), item.get("case_id"), case_id)
        return HistoricalFact(
            category=category,
            value=_normalize_value(value),
            raw_value=value,
            source_case_id=source_case_id,
            source_case_ids=[source_case_id] if source_case_id else [],
            evidence_ids=evidence_ids,
            basis=str(item.get("basis") or "OBSERVED"),
        )
    value = str(item or "").strip()
    category = _category(default_category)
    if not category or not value:
        return None
    return HistoricalFact(
        category=category,
        value=_normalize_value(value),
        raw_value=value,
        source_case_id=case_id,
        source_case_ids=[case_id] if case_id else [],
        basis="OBSERVED",
    )


def _facts_from_evidence_metadata(case_id: str | None, payload: dict[str, Any], evidence: list[EvidenceRecord]) -> list[HistoricalFact]:
    result: list[HistoricalFact] = []
    raw_items = []
    for key in ("evidence", "sources"):
        values = payload.get(key)
        if isinstance(values, list):
            raw_items.extend(item for item in values if isinstance(item, dict))
    for item in raw_items:
        evidence_id = _evidence_id_from_raw(case_id, item)
        for raw_category, category in CATEGORY_ALIASES.items():
            if raw_category not in item:
                continue
            for value in _as_items(item.get(raw_category)):
                fact = _fact_from_item(case_id, value, category)
                if fact and evidence_id and not fact.evidence_ids:
                    fact.evidence_ids.append(evidence_id)
                if fact:
                    result.append(fact)
        for field, category in (("document_type", "document"), ("document_title", "document"), ("reference", "reference"), ("outcome", "outcome"), ("constraint", "constraint")):
            if item.get(field):
                fact = _fact_from_item(case_id, {"value": item.get(field), "evidence_ids": [evidence_id] if evidence_id else []}, category)
                if fact:
                    result.append(fact)
    for record in evidence:
        descriptor = record.source_descriptor or {}
        for field, category in (("document_type", "document"), ("document_title", "document"), ("reference", "reference"), ("outcome", "outcome"), ("constraint", "constraint")):
            value = getattr(record, field, None) if hasattr(record, field) else descriptor.get(field)
            if value:
                fact = _fact_from_item(record.case_id or case_id, {"value": value, "evidence_ids": [_evidence_id(record)]}, category)
                if fact:
                    result.append(fact)
    return result


def _json_from_answer(answer: str) -> tuple[Any | None, str | None]:
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", answer, flags=re.IGNORECASE | re.DOTALL)
    candidates = [item for item in fenced if item.strip().startswith("{")]
    stripped = answer.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.insert(0, stripped)
    if not candidates:
        return None, None
    for candidate in candidates:
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError:
            continue
    return None, "mro_kb_answer_json_block_invalid"


def _proposed_scope(state: AssessmentState, selected: list[SelectedSimilarCase], facts: list[HistoricalFact]) -> list[HistoricalFact]:
    direct_cases = {case.case_id for case in selected if _is_direct_case(case)}
    repeated: dict[tuple[str, str], list[HistoricalFact]] = defaultdict(list)
    for fact in facts:
        if fact.category in {"activity", "calculation", "document", "customer_input", "discipline", "reference", "constraint"}:
            repeated[(fact.category, fact.value)].append(fact)
    scope: list[HistoricalFact] = []
    for (category, value), group in repeated.items():
        source_ids = sorted({case for fact in group for case in (fact.source_case_ids or ([fact.source_case_id] if fact.source_case_id else [])) if case})
        evidence_ids = sorted({evidence_id for fact in group for evidence_id in fact.evidence_ids})
        direct = bool(direct_cases & set(source_ids))
        if direct or len(source_ids) > 1:
            scope.append(HistoricalFact(category=category, value=value, raw_value=group[0].raw_value, source_case_id=source_ids[0] if source_ids else None, source_case_ids=source_ids, evidence_ids=evidence_ids, basis="HISTORICAL_ANALOG"))
    for deliverable in state.business_context.requested_deliverables:
        value = _normalize_value(str(deliverable))
        if not any(item.category == "document" and item.value == value for item in scope):
            scope.append(HistoricalFact(category="document", value=value, raw_value=str(deliverable), basis="CUSTOMER_REQUESTED"))
    return _dedupe_facts(scope)


def _differences(selected: list[SelectedSimilarCase], ctx: NormalizedAtaContext) -> list[str]:
    result: list[str] = []
    for case in selected:
        src = case.source or {}
        for label, current_values, keys in (
            ("другая модель ВС", [ctx.aircraft_model], ("aircraft_model", "aircraft_type", "model")),
            ("другой work type", [ctx.work_type], ("work_type", "maintenance_action")),
            ("другой объект", ctx.physical_objects + ctx.structural_elements, ("object", "component", "physical_object")),
            ("другое повреждение или событие", ctx.damage_types + ctx.damage_descriptions, ("damage_type", "defect_type", "defect", "damage")),
            ("другая зона", ctx.locations, ("zone", "location")),
        ):
            historical = _first(*(src.get(key) for key in keys))
            if historical and current_values and not _matches_any(historical, current_values):
                result.append(f"{label} в {case.case_id}: исторически '{historical}', в новой заявке '{', '.join(str(v) for v in current_values if v)}'.")
        historical_ata = _list_from(src.get("ata") or src.get("affected_ata"))
        if historical_ata and ctx.affected_ata and not set(_keys(historical_ata)) & set(_keys(ctx.affected_ata)):
            result.append(f"другая ATA в {case.case_id}: исторически {', '.join(historical_ata)}, в новой заявке {', '.join(ctx.affected_ata)}.")
    return _unique_text(result)[:8]


def _assumptions(inference: HistoricalInference, selected: list[SelectedSimilarCase]) -> list[str]:
    result: list[str] = []
    if inference.historical_support == "DIRECT":
        result.append("Предположение: подтверждённые исторические работы технически применимы только после отдельной проверки отличий новой заявки.")
    elif inference.historical_support == "PARTIAL":
        result.append("Предположение: найденные частичные материалы могут помочь определить scope, но не подтверждают полный work package.")
    if selected and not inference.facts:
        result.append("Предположение: similarity candidates требуют ручной проверки, потому что документы не дали извлекаемых фактов.")
    return result


def _missing_inputs(state: AssessmentState, ctx: NormalizedAtaContext) -> list[str]:
    result = [f"{item.field}: {item.reason}" for item in state.missing_information]
    result.extend(ctx.uncertainties)
    if not ctx.damage_descriptions and not ctx.damage_types:
        result.append("damage details are not confirmed")
    if not ctx.locations and not state.confirmed_inputs.zone:
        result.append("zone/location is not confirmed")
    return _unique_text(result)[:10]


def _has_direct_support(selected: list[SelectedSimilarCase], facts: list[HistoricalFact]) -> bool:
    direct_cases = {case.case_id for case in selected if _is_direct_case(case)}
    fact_cases = {case for fact in facts for case in (fact.source_case_ids or ([fact.source_case_id] if fact.source_case_id else [])) if case}
    return bool(direct_cases & fact_cases)


def _is_direct_case(case: SelectedSimilarCase) -> bool:
    return case.similarity_class in {"same_identifier", "same_component_defect_zone", "same_component_defect"}


def _category(value: Any) -> str | None:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    category = CATEGORY_ALIASES.get(key, key)
    return category if category in FACT_CATEGORIES else None


def _normalize_value(value: str) -> str:
    raw = " ".join(value.strip().split())
    key = raw.lower()
    return SAFE_SYNONYMS.get(key, key)


def _dedupe_facts(facts: list[HistoricalFact]) -> list[HistoricalFact]:
    by_key: dict[tuple[str, str, str], HistoricalFact] = {}
    for fact in facts:
        if not fact.value.strip() or fact.category not in FACT_CATEGORIES:
            continue
        source = fact.source_case_id or ",".join(fact.source_case_ids)
        key = (fact.category, fact.value.strip().lower(), source)
        existing = by_key.get(key)
        if existing:
            existing.evidence_ids = sorted(set(existing.evidence_ids + fact.evidence_ids))
            existing.source_case_ids = sorted(set(existing.source_case_ids + fact.source_case_ids))
        else:
            by_key[key] = fact
    return list(by_key.values())


def _evidence_ids_from_item(item: dict[str, Any]) -> list[str]:
    raw = item.get("evidence_ids")
    result = [str(value) for value in raw if str(value).strip()] if isinstance(raw, list) else []
    if item.get("document_id") and item.get("chunk_id"):
        case_id = _first(item.get("source_case_id"), item.get("case_id"))
        result.append("|".join(str(value or "") for value in (case_id, item.get("document_id"), item.get("chunk_id"))))
    return sorted(set(result))


def _evidence_id_from_raw(case_id: str | None, item: dict[str, Any]) -> str | None:
    document_id = _first(item.get("document_id"), item.get("doc_id"), item.get("source_document_id"), item.get("id"))
    chunk_id = _first(item.get("chunk_id"))
    if document_id and chunk_id:
        return "|".join(str(value or "") for value in (_first(item.get("case_id"), case_id), document_id, chunk_id))
    return None


def _evidence_id(item: EvidenceRecord) -> str:
    return "|".join(str(value or "") for value in (item.case_id, item.document_id or item.source_document_id, item.chunk_id))


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if value is None:
        return []
    return [value]


def _raw_sources_count(payload: dict[str, Any]) -> int:
    return sum(len(payload.get(key) or []) for key in ("sources", "evidence") if isinstance(payload.get(key), list))


def _fields(state: AssessmentState) -> dict[str, object]:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    fields.update(state.confirmed_additional_data)
    return fields


def _first(*values: Any) -> str | None:
    return next((str(value).strip() for value in values if str(value or "").strip()), None)


def _list_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _matches_any(value: str, candidates: list[str | None]) -> bool:
    key = value.lower()
    return any(str(candidate or "").lower() in key or key in str(candidate or "").lower() for candidate in candidates if candidate)


def _keys(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value.strip()]


def _unique_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value.strip())
    return result
