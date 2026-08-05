from __future__ import annotations

from typing import Any

from .models import AssessmentState, MissingImportance, MissingInformation


def assess_completeness(state: AssessmentState) -> list[MissingInformation]:
    result: list[MissingInformation] = []
    ata = state.ata_impact or {}
    required = ata.get("required_input_data") if isinstance(ata, dict) else []
    if isinstance(required, list):
        for item in required:
            parsed = _from_required_item(item)
            if parsed:
                result.append(parsed)
    uncertainties = (((ata.get("engineering_facts") or {}) if isinstance(ata.get("engineering_facts"), dict) else {}).get("uncertainties") if isinstance(ata, dict) else [])
    if isinstance(uncertainties, list):
        for text in uncertainties:
            lower = str(text).lower()
            if "msn" in lower and not state.confirmed_inputs.msn:
                result.append(_msn_missing("ata_impact", str(text)))
            elif "dimension" in lower or "размер" in lower:
                result.append(
                    MissingInformation(
                        code="DAMAGE_DIMENSIONS_MISSING",
                        field="damage.dimensions",
                        category="damage_definition",
                        importance=MissingImportance.BLOCKING,
                        blocking_stage="APPLICABILITY_CHECK",
                        reason="Размеры повреждения необходимы для проверки применимости.",
                        source="ata_impact",
                    )
                )
    if not state.confirmed_inputs.msn:
        text = (state.source.request_text or "").lower()
        required_text = "msn" in text or "effectivity" in text or "эффектив" in text
        if required_text:
            result.append(_msn_missing("business_context", "MSN необходим для проверки effectivity."))
    deduped: dict[tuple[str, str], MissingInformation] = {}
    for item in result:
        deduped[(item.code, item.field)] = item
    return list(deduped.values())


def has_blocking_customer_missing(items: list[MissingInformation]) -> bool:
    return any(item.importance == MissingImportance.BLOCKING and item.target == "CUSTOMER" for item in items)


def _from_required_item(item: Any) -> MissingInformation | None:
    if isinstance(item, str):
        lower = item.lower()
        if "msn" in lower:
            return _msn_missing("ata_impact", item)
        return MissingInformation(
            code=_code_from_text(item),
            field="request.additional_data",
            category="technical_input",
            importance=MissingImportance.BLOCKING,
            blocking_stage="APPLICABILITY_CHECK",
            reason=item,
            source="ata_impact",
        )
    if isinstance(item, dict):
        field = str(item.get("field") or item.get("name") or item.get("code") or "request.additional_data")
        code = str(item.get("code") or _code_from_text(field))
        importance = MissingImportance.BLOCKING if str(item.get("importance") or "required").lower() in {"required", "blocking"} else MissingImportance.REVIEW_REQUIRED
        return MissingInformation(
            code=code,
            field=_normalize_field(field),
            category=str(item.get("category") or "technical_input"),
            importance=importance,
            blocking_stage=str(item.get("blocking_stage") or "APPLICABILITY_CHECK"),
            target=str(item.get("target") or "CUSTOMER"),
            reason=str(item.get("reason") or item.get("description") or field),
            source="ata_impact",
        )
    return None


def _msn_missing(source: str, reason: str) -> MissingInformation:
    return MissingInformation(
        code="AIRCRAFT_MSN_MISSING",
        field="aircraft.msn",
        category="aircraft_identification",
        importance=MissingImportance.BLOCKING,
        blocking_stage="APPLICABILITY_CHECK",
        target="CUSTOMER",
        reason=reason or "MSN необходим для проверки effectivity.",
        source=source,
    )


def _normalize_field(field: str) -> str:
    return "aircraft.msn" if field.lower() in {"msn", "aircraft_msn", "aircraft.msn"} else field


def _code_from_text(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text.upper()).strip("_")[:64] or "MISSING_INFORMATION"

