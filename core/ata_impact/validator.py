from __future__ import annotations

from typing import Iterable

from .identifiers import normalize_ata
from .models import CRITIC_ACTIONS, EVIDENCE_TYPES, MAPPING_CATEGORIES, RELATION_TYPES, empty_mapping, empty_validated
from .schemas import ATA_CRITIC_SCHEMA, ATA_MAPPING_SCHEMA, ENGINEERING_FACTS_SCHEMA


class ValidationWarning(ValueError):
    pass


def validate_facts(payload: object, identifiers: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    source = payload if isinstance(payload, dict) else {}
    warnings.extend(_schema_warnings(source, ENGINEERING_FACTS_SCHEMA, "engineering_facts"))
    facts: dict[str, object] = {
        "aircraft": _dict(source.get("aircraft")),
        "event": _dict(source.get("event")),
        "physical_objects": _dict_list(source.get("physical_objects")),
        "functional_purposes": _dict_list(source.get("functional_purposes")),
        "locations": _dict_list(source.get("locations")),
        "structural_elements": _dict_list(source.get("structural_elements")),
        "damage": _dict_list(source.get("damage")),
        "relations": _dict_list(source.get("relations")),
        "uncertainties": [str(item) for item in source.get("uncertainties", [])] if isinstance(source.get("uncertainties"), list) else [],
    }
    aircraft = facts["aircraft"]
    if isinstance(aircraft, dict):
        aircraft.setdefault("family", identifiers.get("aircraft_type_raw"))
        aircraft.setdefault("model", None)
        aircraft.setdefault("msn", identifiers.get("msn"))
        aircraft["confidence"] = _confidence(aircraft.get("confidence"), warnings, "aircraft")
    entity_ids = _entity_ids(facts)
    for key in ("physical_objects", "locations", "structural_elements"):
        for item in facts.get(key, []) if isinstance(facts.get(key), list) else []:
            if not item.get("id"):
                warnings.append(f"schema_item_missing_required:{key}:id")
    if len(entity_ids) != sum(
        len([item for item in facts.get(key, []) if isinstance(item, dict) and item.get("id")])
        for key in ("physical_objects", "locations", "structural_elements")
    ):
        warnings.append("duplicate_entity_id")
    valid_purposes: list[dict[str, object]] = []
    for purpose in facts["functional_purposes"] if isinstance(facts["functional_purposes"], list) else []:
        if purpose.get("object_id") not in entity_ids:
            warnings.append(f"invalid_functional_purpose_object:{purpose.get('object_id')}")
            continue
        purpose["confidence"] = _confidence(purpose.get("confidence"), warnings, f"purpose:{purpose.get('object_id')}")
        valid_purposes.append(purpose)
    facts["functional_purposes"] = valid_purposes
    valid_damage: list[dict[str, object]] = []
    for damage in facts["damage"] if isinstance(facts["damage"], list) else []:
        if damage.get("affected_entity_id") not in entity_ids:
            warnings.append(f"invalid_damage_entity:{damage.get('affected_entity_id')}")
            continue
        valid_damage.append(damage)
    facts["damage"] = valid_damage
    valid_relations: list[dict[str, object]] = []
    relation_ids_seen: set[str] = set()
    for relation in facts["relations"] if isinstance(facts["relations"], list) else []:
        relation_id = str(relation.get("id") or "")
        if not relation_id:
            warnings.append("schema_item_missing_required:relations:id")
            continue
        if relation_id in relation_ids_seen:
            warnings.append(f"duplicate_relation_id:{relation_id}")
            continue
        relation_ids_seen.add(relation_id)
        relation_type = str(relation.get("relation") or "")
        if not all(key in relation for key in ("source_entity_id", "target_entity_id", "relation", "evidence_type", "confidence")):
            warnings.append(f"schema_item_missing_required:relations:{relation_id}")
            continue
        if relation_type not in RELATION_TYPES:
            warnings.append(f"invalid_relation_type:{relation_type}")
            continue
        if relation.get("source_entity_id") not in entity_ids or relation.get("target_entity_id") not in entity_ids:
            warnings.append(f"invalid_relation_entity:{relation.get('id')}")
            continue
        evidence_type = str(relation.get("evidence_type") or "inferred")
        if evidence_type not in EVIDENCE_TYPES:
            relation["evidence_type"] = "inferred"
            warnings.append(f"invalid_evidence_type:{relation.get('id')}")
        relation["confidence"] = _confidence(relation.get("confidence"), warnings, str(relation.get("id") or "relation"))
        valid_relations.append(relation)
    facts["relations"] = valid_relations
    return facts, warnings


def validate_mapping(
    payload: object,
    facts: dict[str, object],
    declared: list[str],
    request: str = "",
) -> tuple[dict[str, list[dict[str, object]]], list[str]]:
    warnings: list[str] = []
    raw = payload if isinstance(payload, dict) else {}
    warnings.extend(_schema_warnings(raw, ATA_MAPPING_SCHEMA, "ata_mapping"))
    mapping = empty_mapping()
    entity_ids = _entity_ids(facts)
    object_ids = _ids_for(facts, "physical_objects")
    location_ids = _ids_for(facts, "locations")
    structure_ids = _ids_for(facts, "structural_elements")
    damaged_ids = {
        str(item.get("affected_entity_id"))
        for item in facts.get("damage", [])
        if isinstance(item, dict) and item.get("affected_entity_id")
    }
    event = facts.get("event", {})
    if isinstance(event, dict):
        damaged_ids.update(
            str(item) for item in event.get("target_entity_ids", []) if str(item) in entity_ids
        )
    for item in facts.get("physical_objects", []):
        if not isinstance(item, dict):
            continue
        involvement = str(item.get("involvement") or "").lower()
        if item.get("damage_confirmed") is True or involvement in {"damaged", "inspected", "changed", "modified", "removed", "replaced", "work_target"}:
            damaged_ids.add(str(item.get("id")))
    structural_involved_ids = set(damaged_ids)
    for item in facts.get("structural_elements", []):
        if not isinstance(item, dict):
            continue
        involvement = str(item.get("involvement") or "").lower()
        if item.get("damage_confirmed") is True or involvement in {"damaged", "repair", "repaired", "modified", "changed", "removed", "replaced"}:
            structural_involved_ids.add(str(item.get("id")))
    relation_ids = {str(item.get("id")) for item in facts.get("relations", []) if isinstance(item, dict) and item.get("id")}
    for category in MAPPING_CATEGORIES:
        seen: set[tuple[str, str, str]] = set()
        for item in _dict_list(raw.get(category)):
            ata = normalize_ata(item.get("ata"))
            if not ata:
                warnings.append(f"invalid_ata:{category}")
                continue
            if "confidence" not in item or not str(item.get("reason") or "").strip():
                warnings.append(f"mapping_item_missing_required:{category}:{ata}")
                continue
            item["ata"] = ata
            item["confidence"] = _confidence(item.get("confidence"), warnings, f"{category}:{ata}")
            entity_id = str(item.get("entity_id") or "")
            relation_id = str(item.get("relation_id") or "")
            if category in {"object_ata", "structural_ata", "location_context_ata"} and not entity_id:
                warnings.append(f"missing_entity_id:{category}:{ata}")
                continue
            if entity_id and entity_id not in entity_ids:
                warnings.append(f"invalid_entity_id:{category}:{ata}")
                continue
            if category == "object_ata" and (entity_id not in object_ids or entity_id not in damaged_ids):
                warnings.append(f"object_ata_wrong_entity_role:{ata}")
                continue
            if category == "structural_ata" and (entity_id not in structure_ids or entity_id not in structural_involved_ids):
                warnings.append(f"structural_ata_without_involvement:{ata}")
                continue
            if category == "location_context_ata":
                is_location = entity_id in location_ids
                is_location_structure = entity_id in structure_ids and entity_id not in structural_involved_ids
                if not (is_location or is_location_structure):
                    warnings.append(f"location_context_wrong_entity_role:{ata}")
                    continue
            if category == "interface_ata_hypotheses" and (not relation_id or relation_id not in relation_ids):
                warnings.append(f"interface_without_valid_relation:{ata}")
                continue
            if category == "interface_ata_hypotheses":
                relation = next(item for item in facts.get("relations", []) if isinstance(item, dict) and item.get("id") == relation_id)
                if relation.get("relation") == "location_reference":
                    warnings.append(f"non_interface_relation:{ata}:{relation_id}")
                    continue
                if relation.get("relation") == "adjacent_to":
                    condition = " ".join(str(item.get(key) or "") for key in ("condition", "reason", "source_fragment")).lower()
                    if not any(term in condition for term in ("access", "protect", "доступ", "защит")):
                        warnings.append(f"adjacent_without_access_or_protection:{ata}:{relation_id}")
                        continue
            if category == "procedure_ata_hypotheses" and not (
                relation_id in relation_ids
                or entity_id in entity_ids
                or _fragment_in_request(str(item.get("source_fragment") or ""), request)
            ):
                warnings.append(f"procedure_without_factual_anchor:{ata}")
                continue
            key = (ata, entity_id, relation_id)
            if key not in seen:
                mapping[category].append(item)
                seen.add(key)
    present_declared = {item["ata"] for item in mapping["user_declared_ata"]}
    for ata in declared:
        if ata not in present_declared:
            mapping["user_declared_ata"].append(
                {
                    "ata": ata,
                    "confidence": 1.0,
                    "reason": "Explicitly declared in the request; not semantically verified",
                    "status": "unverified",
                    "source_fragment": ata,
                }
            )
    allowed_user_status = {"consistent", "conflicting", "unverified", "not_in_certificate"}
    for item in mapping["user_declared_ata"]:
        if str(item.get("status") or "") not in allowed_user_status:
            item["status"] = "unverified"
            warnings.append(f"invalid_user_declared_status:{item['ata']}")
    return mapping, warnings


def validate_critic(
    payload: object,
    mapping: dict[str, list[dict[str, object]]] | None = None,
    facts: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    raw = payload if isinstance(payload, dict) else {}
    actions = raw.get("actions", [])
    warnings: list[str] = []
    warnings.extend(_schema_warnings(raw, ATA_CRITIC_SCHEMA, "ata_critic"))
    result: list[dict[str, object]] = []
    for item in _dict_list(actions):
        action = str(item.get("action") or "")
        ata = normalize_ata(item.get("ata"))
        if action not in CRITIC_ACTIONS or not ata:
            warnings.append("invalid_critic_action")
            continue
        if not str(item.get("reason") or "").strip():
            warnings.append(f"critic_action_missing_reason:{ata}")
            continue
        category = str(item.get("category") or "")
        if category not in MAPPING_CATEGORIES:
            warnings.append(f"invalid_critic_category:{category}")
            continue
        item["ata"] = ata
        if action != "add_missing_candidate" and mapping is not None:
            matches = [
                candidate
                for candidate in mapping[category]
                if candidate.get("ata") == ata
                and (not item.get("entity_id") or candidate.get("entity_id") == item.get("entity_id"))
                and (
                    category in {"object_ata", "structural_ata"}
                    or not item.get("relation_id")
                    or candidate.get("relation_id") == item.get("relation_id")
                )
            ]
            if len(matches) != 1:
                warnings.append(f"orphan_or_ambiguous_critic_action:{category}:{ata}")
                continue
            item.setdefault("entity_id", matches[0].get("entity_id"))
            item.setdefault("relation_id", matches[0].get("relation_id"))
            if action == "downgrade_to_location_context" and category not in {
                "object_ata",
                "structural_ata",
                "location_context_ata",
            }:
                warnings.append(f"incompatible_critic_action:{action}:{category}:{ata}")
                continue
            if action == "downgrade_to_possible" and category not in {
                "object_ata",
                "structural_ata",
                "interface_ata_hypotheses",
                "procedure_ata_hypotheses",
            }:
                warnings.append(f"incompatible_critic_action:{action}:{category}:{ata}")
                continue
            if action == "downgrade_to_possible" and category in {"object_ata", "structural_ata"}:
                relation = next(
                    (
                        relation
                        for relation in (facts or {}).get("relations", [])
                        if isinstance(relation, dict) and relation.get("id") == item.get("relation_id")
                    ),
                    None,
                )
                candidate_entity = str(matches[0].get("entity_id") or "")
                if (
                    relation is None
                    or relation.get("relation") in {"location_reference"}
                    or candidate_entity not in {str(relation.get("source_entity_id") or ""), str(relation.get("target_entity_id") or "")}
                ):
                    warnings.append(f"incompatible_critic_action:{action}:{category}:{ata}")
                    continue
        result.append(item)
    return result, warnings


def apply_critic_additions(
    mapping: dict[str, list[dict[str, object]]],
    actions: list[dict[str, object]],
    facts: dict[str, object],
    request: str = "",
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]], list[str]]:
    """Route critic additions through the exact same role/reference validation."""
    accepted_actions: list[dict[str, object]] = []
    warnings: list[str] = []
    for action in actions:
        if action.get("action") != "add_missing_candidate":
            accepted_actions.append(action)
            continue
        category = str(action.get("category") or "")
        candidate = {
            key: value
            for key, value in action.items()
            if key in {"ata", "entity_id", "relation_id", "confidence", "reason", "source_fragment", "condition", "basis", "status"}
        }
        candidate.setdefault("confidence", 0.5)
        candidate.setdefault("reason", str(action.get("reason") or "Candidate added by critic"))
        trial, trial_warnings = validate_mapping({category: [candidate], **{key: [] for key in MAPPING_CATEGORIES if key != category}}, facts, [], request)
        warnings.extend(trial_warnings)
        if trial.get(category):
            existing = {
                (item.get("ata"), item.get("entity_id"), item.get("relation_id"))
                for item in mapping[category]
            }
            mapping[category].extend(
                item
                for item in trial[category]
                if (item.get("ata"), item.get("entity_id"), item.get("relation_id")) not in existing
            )
            accepted_actions.append(action)
        else:
            warnings.append(f"critic_addition_rejected:{category}:{action.get('ata')}")
    return mapping, accepted_actions, warnings


def assemble(
    mapping: dict[str, list[dict[str, object]]],
    critic_actions: list[dict[str, object]],
    certificate_validation: list[dict[str, object]],
    document_verification: dict[str, object],
) -> dict[str, object]:
    validated = empty_validated()
    default_status = {
        "object_ata": "inferred_from_request",
        "structural_ata": "direct_confirmed",
        "location_context_ata": "location_context",
        "interface_ata_hypotheses": "possible_interface",
        "procedure_ata_hypotheses": "possible_procedure",
        "user_declared_ata": "user_declared_unverified",
    }
    for category in MAPPING_CATEGORIES:
        for item in mapping[category]:
            action = _action_for_item(critic_actions, category, item)
            status = default_status[category]
            if action:
                verb = action["action"]
                if verb == "reject":
                    status = "rejected"
                elif verb == "downgrade_to_location_context":
                    status = "location_context"
                    if category == "structural_ata":
                        item["critic_fact_conflict"] = True
                elif verb in {"downgrade_to_possible", "require_document"}:
                    if category == "procedure_ata_hypotheses":
                        status = "possible_procedure"
                    elif category == "interface_ata_hypotheses":
                        status = "possible_interface"
                    elif verb == "downgrade_to_possible" and action.get("relation_id"):
                        status = "possible_interface"
                    elif verb == "require_document":
                        # Keep the fact-derived category visible, but flag that
                        # the conclusion still requires controlled evidence.
                        status = default_status[category]
                        item["document_required"] = True
                    else:
                        status = "rejected"
                elif verb == "confirm" and category in {"object_ata", "structural_ata"}:
                    status = "direct_confirmed"
            validated[status].append(_trace_item(item, category, action, certificate_validation))

    documents = [item for item in document_verification.get("documents", []) if isinstance(item, dict)]
    for document in documents:
        if not _controlled_applicable(document):
            continue
        for confirmation in _document_confirmations(document):
            source = _find_confirmed_candidate(validated, confirmation)
            if source:
                source_key = (
                    source.get("ata"),
                    source.get("mapping_category"),
                    source.get("entity_id"),
                    source.get("relation_id"),
                )
                for status, items in validated.items():
                    if status == "document_confirmed":
                        continue
                    validated[status] = [
                        item
                        for item in items
                        if (
                            item.get("ata"),
                            item.get("mapping_category"),
                            item.get("entity_id"),
                            item.get("relation_id"),
                        )
                        != source_key
                    ]
                validated["document_confirmed"].append(
                    {**source, "status": "document_confirmed", "document_evidence": [_document_ref(document)]}
                )
    _dedupe_validated(validated)
    affected = _atas(validated, ("direct_confirmed", "inferred_from_request", "document_confirmed"))
    potential = _atas(validated, ("possible_interface", "possible_procedure"))
    context = _atas(validated, ("location_context",))
    return {
        "validated_ata": validated,
        "affected_ata": affected,
        # An ATA can legitimately have more than one role. Do not hide its
        # interface/procedure or location role merely because another entity
        # of the same chapter is directly affected.
        "potentially_affected_ata": potential,
        "context_ata": context,
    }


def _trace_item(
    item: dict[str, object],
    category: str,
    action: dict[str, object] | None,
    certificate: list[dict[str, object]],
) -> dict[str, object]:
    scope = next((entry for entry in certificate if entry.get("ata") == item.get("ata")), None)
    return {
        **item,
        "mapping_category": category,
        "critic_action": action.get("action") if action else "not_run",
        "critic_reason": action.get("reason") if action else None,
        **({"relation_id": action.get("relation_id")} if action and action.get("relation_id") else {}),
        "certificate_scope": scope,
        "document_evidence": list(item.get("document_evidence") or []),
    }


def _controlled_applicable(document: dict[str, object]) -> bool:
    trust = str(document.get("trust_level") or "").lower()
    required_provenance = ("document_id", "document_type", "revision", "effectivity", "section_reference")
    return (
        trust in {"controlled_oem", "approved_data"}
        and document.get("applicable") is True
        and document.get("current_revision") is True
        and str(document.get("verification_status") or "").lower() == "confirmed"
        and all(document.get(key) not in (None, "") for key in required_provenance)
    )


def _document_confirmations(document: dict[str, object]) -> list[dict[str, object]]:
    records = document.get("confirmed_candidates")
    if not isinstance(records, list):
        return []
    result: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        ata = normalize_ata(record.get("ata"))
        category = str(record.get("category") or "")
        anchor_valid = (
            bool(record.get("entity_id"))
            if category in {"object_ata", "structural_ata"}
            else bool(record.get("relation_id"))
            if category == "interface_ata_hypotheses"
            else bool(record.get("entity_id") or record.get("relation_id") or record.get("source_fragment"))
        )
        if ata and anchor_valid and category in MAPPING_CATEGORIES and category not in {"location_context_ata", "user_declared_ata"}:
            result.append({**record, "ata": ata, "category": category})
    return result


def _document_ref(document: dict[str, object]) -> dict[str, object]:
    return {
        key: document.get(key)
        for key in ("document_id", "document_type", "revision", "effectivity", "section_reference", "title")
        if document.get(key) not in (None, "")
    }


def _find_confirmed_candidate(
    validated: dict[str, list[dict[str, object]]],
    confirmation: dict[str, object],
) -> dict[str, object] | None:
    matches: list[dict[str, object]] = []
    for status, items in validated.items():
        if status in {"rejected", "document_confirmed"}:
            continue
        for item in items:
            if (
                item.get("ata") == confirmation.get("ata")
                and item.get("mapping_category") == confirmation.get("category")
                and (not confirmation.get("entity_id") or item.get("entity_id") == confirmation.get("entity_id"))
                and (not confirmation.get("relation_id") or item.get("relation_id") == confirmation.get("relation_id"))
                and (not confirmation.get("source_fragment") or item.get("source_fragment") == confirmation.get("source_fragment"))
            ):
                matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _action_for_item(
    actions: list[dict[str, object]], category: str, item: dict[str, object]
) -> dict[str, object] | None:
    matches = [
        action
        for action in actions
        if action.get("category") == category
        and action.get("ata") == item.get("ata")
        and (not action.get("entity_id") or action.get("entity_id") == item.get("entity_id"))
        and (
            category in {"object_ata", "structural_ata"}
            or not action.get("relation_id")
            or action.get("relation_id") == item.get("relation_id")
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _dedupe_validated(validated: dict[str, list[dict[str, object]]]) -> None:
    for status, items in validated.items():
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict[str, object]] = []
        for item in items:
            key = (str(item.get("ata")), str(item.get("entity_id") or ""), str(item.get("relation_id") or ""))
            if key not in seen:
                item["status"] = status
                unique.append(item)
                seen.add(key)
        validated[status] = unique


def _atas(validated: dict[str, list[dict[str, object]]], statuses: Iterable[str]) -> list[str]:
    return sorted({str(item["ata"]) for status in statuses for item in validated[status] if item.get("ata")})


def _entity_ids(facts: dict[str, object]) -> set[str]:
    ids: set[str] = set()
    for key in ("physical_objects", "locations", "structural_elements"):
        for item in facts.get(key, []) if isinstance(facts.get(key), list) else []:
            if isinstance(item, dict) and item.get("id"):
                ids.add(str(item["id"]))
    return ids


def _ids_for(facts: dict[str, object], key: str) -> set[str]:
    return {
        str(item["id"])
        for item in facts.get(key, [])
        if isinstance(item, dict) and item.get("id")
    }


def _confidence(value: object, warnings: list[str], label: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        warnings.append(f"invalid_confidence:{label}")
        return 0.0
    if not 0.0 <= confidence <= 1.0:
        warnings.append(f"confidence_out_of_range:{label}")
    return min(1.0, max(0.0, confidence))


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _schema_warnings(payload: dict[str, object], schema: dict[str, object], label: str) -> list[str]:
    """Dependency-free validation for the checked-in JSON Schema contracts."""
    warnings: list[str] = []
    for key in schema.get("required", []):
        if key not in payload:
            warnings.append(f"schema_missing_required:{label}:{key}")
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for key, definition in properties.items():
            if key not in payload or not isinstance(definition, dict):
                continue
            expected = definition.get("type")
            if expected == "array" and not isinstance(payload[key], list):
                warnings.append(f"schema_type_error:{label}:{key}:array")
            elif expected == "object" and not isinstance(payload[key], dict):
                warnings.append(f"schema_type_error:{label}:{key}:object")
    return warnings


def _fragment_in_request(fragment: str, request: str) -> bool:
    normalized_fragment = " ".join(fragment.lower().split())
    normalized_request = " ".join(request.lower().split())
    return bool(normalized_fragment) and normalized_fragment in normalized_request
