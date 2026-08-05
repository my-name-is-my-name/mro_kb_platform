from __future__ import annotations

from typing import Any

from ._pydantic import BaseModel, Field


class NormalizedAtaContext(BaseModel):
    aircraft_family: str | None = None
    aircraft_model: str | None = None
    msn: str | None = None
    work_type: str | None = None
    maintenance_action: str | None = None
    physical_objects: list[str] = Field(default_factory=list)
    structural_elements: list[str] = Field(default_factory=list)
    damage_types: list[str] = Field(default_factory=list)
    damage_descriptions: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    affected_ata: list[str] = Field(default_factory=list)
    potentially_affected_ata: list[str] = Field(default_factory=list)
    context_ata: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


def normalize_ata_context(ata_impact: dict[str, Any] | None, fields: dict[str, Any] | None = None, requested_deliverables: list[str] | None = None, request_text: str = "") -> NormalizedAtaContext:
    ata = ata_impact if isinstance(ata_impact, dict) else {}
    fields = fields or {}
    facts = ata.get("engineering_facts") if isinstance(ata.get("engineering_facts"), dict) else {}
    event = facts.get("event") if isinstance(facts.get("event"), dict) else {}
    aircraft_model = _first(fields.get("aircraft_model"), fields.get("aircraft_type"), _find_identifier(ata.get("identifiers"), {"aircraft_model", "aircraft_type"}))
    return NormalizedAtaContext(
        aircraft_family=_family(aircraft_model),
        aircraft_model=aircraft_model,
        msn=_first(fields.get("msn"), _find_identifier(ata.get("identifiers"), {"msn"})),
        work_type=_infer_work_type(fields, facts, event, requested_deliverables or [], request_text),
        maintenance_action=_text(event.get("maintenance_action")),
        physical_objects=_unique(_collect_names(facts.get("physical_objects")) + _collect_legacy(facts, ("object", "component", "damaged_object"))),
        structural_elements=_unique(_collect_names(facts.get("structural_elements"))),
        damage_types=_unique(_collect_damage_types(facts.get("damage")) + _collect_legacy(facts, ("damage_type", "defect"))),
        damage_descriptions=_unique(_collect_damage_descriptions(facts.get("damage")) + _collect_legacy(facts, ("damage",))),
        locations=_unique(_collect_names(facts.get("locations")) + _collect_legacy(facts, ("zone", "location")) + _as_list(fields.get("zone"))),
        affected_ata=[str(item) for item in ata.get("affected_ata", []) if str(item).strip()] if isinstance(ata.get("affected_ata"), list) else [],
        potentially_affected_ata=[str(item) for item in ata.get("potentially_affected_ata", []) if str(item).strip()] if isinstance(ata.get("potentially_affected_ata"), list) else [],
        context_ata=[str(item) for item in ata.get("context_ata", []) if str(item).strip()] if isinstance(ata.get("context_ata"), list) else [],
        identifiers=_unique(_collect_identifiers(ata.get("identifiers"))),
        uncertainties=[str(item) for item in facts.get("uncertainties", [])] if isinstance(facts.get("uncertainties"), list) else [],
    )


def _infer_work_type(fields: dict[str, Any], facts: dict[str, Any], event: dict[str, Any], deliverables: list[str], text: str) -> str | None:
    explicit = _first(fields.get("work_type"), fields.get("maintenance_action"), event.get("type"), event.get("maintenance_action"))
    mapped = _map_work_type(explicit)
    if mapped:
        return mapped
    deliverable_set = {str(item).lower() for item in deliverables}
    if {"repair_drawing", "repair_instruction", "stress_substantiation", "approved_repair_data"} & deliverable_set:
        return "REPAIR_DESIGN"
    if "damage_assessment" in deliverable_set:
        return "DAMAGE_ASSESSMENT"
    lower = text.lower()
    lexical = (
        ("amoc", "AMOC"),
        ("modification", "MODIFICATION"),
        ("модификац", "MODIFICATION"),
        ("inspection program", "INSPECTION_PROGRAM"),
        ("document review", "DOCUMENT_REVIEW"),
        ("component repair", "COMPONENT_REPAIR"),
        ("certification", "CERTIFICATION_SUPPORT"),
        ("repair design", "REPAIR_DESIGN"),
        ("develop repair", "REPAIR_DESIGN"),
        ("ремонт", "REPAIR_DESIGN"),
    )
    return next((value for marker, value in lexical if marker in lower), None)


def _map_work_type(value: Any) -> str | None:
    text = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    mapping = {
        "REPAIR": "REPAIR_DESIGN",
        "REPAIR_DESIGN": "REPAIR_DESIGN",
        "DAMAGE_ASSESSMENT": "DAMAGE_ASSESSMENT",
        "MODIFICATION": "MODIFICATION",
        "AMOC": "AMOC",
        "INSPECTION_PROGRAM": "INSPECTION_PROGRAM",
        "DOCUMENT_REVIEW": "DOCUMENT_REVIEW",
        "COMPONENT_REPAIR": "COMPONENT_REPAIR",
        "CERTIFICATION_SUPPORT": "CERTIFICATION_SUPPORT",
    }
    return mapping.get(text)


def _collect_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.extend(_as_list(item.get("name") or item.get("type") or item.get("description")))
        else:
            result.extend(_as_list(item))
    return result


def _collect_damage_types(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.extend(_as_list(item.get("type") or item.get("damage_type") or item.get("name")))
        else:
            result.extend(_as_list(item))
    return result


def _collect_damage_descriptions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.extend(_as_list(item.get("description") or item.get("details")))
    return result


def _collect_legacy(facts: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for key in keys:
        result.extend(_as_list(facts.get(key)))
    return result


def _collect_identifiers(value: Any) -> list[str]:
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                result.extend(_as_list(item.get("value") or item.get("identifier") or item.get("id")))
            else:
                result.extend(_as_list(item))
        return result
    return _as_list(value)


def _find_identifier(value: Any, keys: set[str]) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and str(item.get("type") or item.get("field") or "").lower() in keys:
            return _text(item.get("value"))
    return None


def _family(value: str | None) -> str | None:
    text = (value or "").upper()
    if any(marker in text for marker in ("A319", "A320", "A321")):
        return "A320"
    if text.startswith("B737"):
        return "B737"
    return text.split("-")[0] if text else None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _first(*values: Any) -> str | None:
    return next((str(value).strip() for value in values if str(value or "").strip()), None)


def _text(value: Any) -> str | None:
    return str(value).strip() if str(value or "").strip() else None


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value.strip())
    return result

