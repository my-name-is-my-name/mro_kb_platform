from __future__ import annotations

import re
from typing import Iterable

from .evidence import is_controlled_evidence_document
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
    candidate_ids: set[str] = set()
    for category in MAPPING_CATEGORIES:
        sequence = 0
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
                if relation.get("relation") in {"location_reference", "installed_in"}:
                    warnings.append(f"non_interface_relation:{ata}:{relation_id}")
                    continue
                if relation.get("relation") == "adjacent_to":
                    interface_basis = str(relation.get("interface_basis") or "")
                    if interface_basis not in {
                        "access_required",
                        "protection_required",
                    }:
                        warnings.append(f"adjacent_without_access_or_protection:{ata}:{relation_id}")
                        continue
            if category == "procedure_ata_hypotheses" and not (
                relation_id in relation_ids
                or entity_id in entity_ids
                or _fragment_in_request(str(item.get("source_fragment") or ""), request)
            ):
                warnings.append(f"procedure_without_factual_anchor:{ata}")
                continue
            sequence += 1
            candidate_id = str(item.get("candidate_id") or "").strip()
            if candidate_id and not _valid_candidate_id(
                candidate_id,
                category,
                ata,
                entity_id,
                relation_id,
            ):
                warnings.append(f"invalid_candidate_id_format:{candidate_id}")
                candidate_id = ""
            if not candidate_id:
                anchor = entity_id or relation_id or "request"
                ata_token = ata.replace(" ", "_").replace("-", "_")
                candidate_id = f"candidate:{category}:{anchor}:{ata_token}:{sequence}"
            if candidate_id in candidate_ids:
                warnings.append(f"duplicate_candidate_id:{candidate_id}")
                continue
            candidate_ids.add(candidate_id)
            item["candidate_id"] = candidate_id
            item["candidate_state"] = "candidate_unverified"
            mapping[category].append(item)
    present_declared = {item["ata"] for item in mapping["user_declared_ata"]}
    for ata in declared:
        if ata not in present_declared:
            sequence = len(mapping["user_declared_ata"]) + 1
            candidate_id = (
                "candidate:user_declared_ata:request:"
                f"{ata.replace(' ', '_').replace('-', '_')}:{sequence}"
            )
            while candidate_id in candidate_ids:
                sequence += 1
                candidate_id = (
                    "candidate:user_declared_ata:request:"
                    f"{ata.replace(' ', '_').replace('-', '_')}:{sequence}"
                )
            candidate_ids.add(candidate_id)
            mapping["user_declared_ata"].append(
                {
                    "candidate_id": candidate_id,
                    "candidate_state": "candidate_unverified",
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
    known_candidates = {
        str(candidate.get("candidate_id")): (category, candidate)
        for category in MAPPING_CATEGORIES
        if category != "user_declared_ata"
        for candidate in (mapping or {}).get(category, [])
        if candidate.get("candidate_id")
    }
    action_counts: dict[str, int] = {}
    reference_counts: dict[str, int] = {}
    coverage_invalid: set[str] = set()
    global_coverage_invalid = False
    for item in _dict_list(actions):
        action = str(item.get("action") or "")
        candidate_id = str(item.get("candidate_id") or "").strip()
        if candidate_id in known_candidates:
            reference_counts[candidate_id] = reference_counts.get(candidate_id, 0) + 1
            if reference_counts[candidate_id] > 1:
                warnings.append(f"duplicate_critic_action:{candidate_id}")
                coverage_invalid.add(candidate_id)
        if action not in CRITIC_ACTIONS or not candidate_id:
            warnings.append("invalid_critic_action")
            if candidate_id in known_candidates:
                coverage_invalid.add(candidate_id)
            else:
                global_coverage_invalid = True
            continue
        if not str(item.get("reason") or "").strip():
            warnings.append(f"critic_action_missing_reason:{candidate_id}")
            if candidate_id in known_candidates:
                coverage_invalid.add(candidate_id)
            else:
                global_coverage_invalid = True
            continue
        if action == "add_missing_candidate":
            category = str(item.get("category") or "")
            ata = normalize_ata(item.get("ata"))
            if category not in MAPPING_CATEGORIES or not ata:
                warnings.append(f"invalid_critic_addition:{candidate_id}")
                continue
            item["ata"] = ata
            result.append(item)
            continue
        target = known_candidates.get(candidate_id)
        if target is None:
            warnings.append(f"unknown_critic_candidate_id:{candidate_id}")
            global_coverage_invalid = True
            continue
        category, candidate = target
        ata = str(candidate.get("ata") or "")
        supplied_ata = normalize_ata(item.get("ata")) if item.get("ata") else ata
        supplied_category = str(item.get("category") or category)
        anchor_mismatch = (
            bool(item.get("entity_id"))
            and bool(candidate.get("entity_id"))
            and item.get("entity_id") != candidate.get("entity_id")
        ) or (
            bool(item.get("relation_id"))
            and bool(candidate.get("relation_id"))
            and item.get("relation_id") != candidate.get("relation_id")
        )
        require_document_anchor_mutation = action == "require_document" and (
            (
                bool(item.get("entity_id"))
                and item.get("entity_id") != candidate.get("entity_id")
            )
            or (
                bool(item.get("relation_id"))
                and item.get("relation_id") != candidate.get("relation_id")
            )
        )
        if (
            supplied_ata != ata
            or supplied_category != category
            or anchor_mismatch
            or require_document_anchor_mutation
        ):
            warnings.append(f"critic_candidate_mismatch:{candidate_id}")
            coverage_invalid.add(candidate_id)
            continue
        item["ata"] = ata
        item["category"] = category
        item.setdefault("entity_id", candidate.get("entity_id"))
        item.setdefault("relation_id", candidate.get("relation_id"))
        action_counts[candidate_id] = action_counts.get(candidate_id, 0) + 1
        if action_counts[candidate_id] > 1:
            coverage_invalid.add(candidate_id)
        if mapping is not None:
            if action == "downgrade_to_location_context" and category not in {
                "object_ata",
                "structural_ata",
                "location_context_ata",
            }:
                warnings.append(f"incompatible_critic_action:{action}:{category}:{candidate_id}")
                coverage_invalid.add(candidate_id)
                continue
            if action == "downgrade_to_possible" and category not in {
                "object_ata",
                "structural_ata",
                "interface_ata_hypotheses",
                "procedure_ata_hypotheses",
            }:
                warnings.append(f"incompatible_critic_action:{action}:{category}:{candidate_id}")
                coverage_invalid.add(candidate_id)
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
                candidate_entity = str(candidate.get("entity_id") or "")
                if (
                    relation is None
                    or relation.get("relation") in {"location_reference", "installed_in"}
                    or candidate_entity not in {str(relation.get("source_entity_id") or ""), str(relation.get("target_entity_id") or "")}
                ):
                    warnings.append(f"incompatible_critic_action:{action}:{category}:{candidate_id}")
                    coverage_invalid.add(candidate_id)
                    continue
        result.append(item)
    for item in result:
        if (
            global_coverage_invalid
            or str(item.get("candidate_id") or "") in coverage_invalid
        ):
            item["coverage_invalid"] = True
    for candidate_id in known_candidates:
        count = action_counts.get(candidate_id, 0)
        if count == 0:
            warnings.append(f"missing_critic_action:{candidate_id}")
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
        existing = {
            str(item.get("candidate_id"))
            for existing_category in MAPPING_CATEGORIES
            for item in mapping[existing_category]
            if item.get("candidate_id")
        }
        if str(action.get("candidate_id") or "") in existing:
            warnings.append(
                f"critic_addition_candidate_id_collision:{action.get('candidate_id')}"
            )
            continue
        candidate = {
            key: value
            for key, value in action.items()
            if key in {"candidate_id", "ata", "entity_id", "relation_id", "confidence", "reason", "source_fragment", "condition", "basis", "status"}
        }
        candidate.setdefault("confidence", 0.5)
        candidate.setdefault("reason", str(action.get("reason") or "Candidate added by critic"))
        trial, trial_warnings = validate_mapping({category: [candidate], **{key: [] for key in MAPPING_CATEGORIES if key != category}}, facts, [], request)
        warnings.extend(trial_warnings)
        if trial.get(category):
            if any(
                str(item.get("candidate_id")) in existing
                for item in trial[category]
            ):
                warnings.append(
                    f"critic_addition_candidate_id_collision:{action.get('candidate_id')}"
                )
                continue
            mapping[category].extend(
                item
                for item in trial[category]
                if str(item.get("candidate_id")) not in existing
            )
            warnings.append(f"critic_addition_requires_review:{action.get('candidate_id')}")
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
    for category in MAPPING_CATEGORIES:
        for item in mapping[category]:
            action = _action_for_item(critic_actions, category, item)
            if category == "user_declared_ata":
                status = "user_declared_unverified"
            elif action is None:
                status = "candidate_unverified"
            else:
                verb = action["action"]
                if verb == "reject":
                    status = "rejected"
                elif verb == "downgrade_to_location_context":
                    status = "location_context"
                    if category == "structural_ata":
                        item["critic_fact_conflict"] = True
                elif verb == "require_document":
                    status = "document_verification_required"
                elif verb == "downgrade_to_possible":
                    if category == "procedure_ata_hypotheses":
                        status = "possible_procedure"
                    elif category == "interface_ata_hypotheses":
                        status = "possible_interface"
                    elif action.get("relation_id"):
                        status = "possible_interface"
                    else:
                        status = "rejected"
                elif verb == "confirm":
                    if category == "object_ata":
                        status = "inferred_from_request"
                    elif category == "structural_ata":
                        status = "direct_confirmed"
                    elif category == "location_context_ata":
                        status = "location_context"
                    elif category == "interface_ata_hypotheses":
                        status = "possible_interface"
                    elif category == "procedure_ata_hypotheses":
                        status = "possible_procedure"
                    else:
                        status = "candidate_unverified"
                else:
                    status = "candidate_unverified"
            validated[status].append(_trace_item(item, category, action, certificate_validation))

    documents = [item for item in document_verification.get("documents", []) if isinstance(item, dict)]
    valid_candidates = {
        str(item.get("candidate_id")): item
        for item in validated["document_verification_required"]
        if item.get("candidate_id")
    }
    for document in documents:
        if not is_controlled_evidence_document(document, valid_candidates):
            continue
        for confirmation in _document_confirmations(document):
            source = _find_confirmed_candidate(validated, confirmation)
            if source:
                candidate_id = source.get("candidate_id")
                validated["document_verification_required"] = [
                    item
                    for item in validated["document_verification_required"]
                    if item.get("candidate_id") != candidate_id
                ]
                validated["document_confirmed"].append(
                    {
                        **source,
                        "status": "document_confirmed",
                        "previous_status": "document_verification_required",
                        "document_evidence": [_document_ref(document)],
                    }
                )
    _dedupe_validated(validated)
    affected = _atas(validated, ("direct_confirmed", "inferred_from_request", "document_confirmed"))
    potential = _atas(
        validated,
        ("possible_interface", "possible_procedure", "document_verification_required"),
    )
    context = _atas(validated, ("location_context",))
    return {
        "validated_ata": validated,
        "affected_ata": affected,
        # An ATA can legitimately have more than one role. Do not hide its
        # interface/procedure or location role merely because another entity
        # of the same chapter is directly affected.
        "potentially_affected_ata": potential,
        "context_ata": context,
        "critic_coverage_complete": not bool(validated["candidate_unverified"]),
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


def _document_confirmations(document: dict[str, object]) -> list[dict[str, object]]:
    records = document.get("confirmed_candidates")
    if not isinstance(records, list):
        return []
    result: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate_id = str(record.get("candidate_id") or "").strip()
        ata = normalize_ata(record.get("ata"))
        category = str(record.get("category") or "")
        anchor_valid = (
            bool(record.get("entity_id"))
            if category in {"object_ata", "structural_ata"}
            else bool(record.get("relation_id"))
            if category == "interface_ata_hypotheses"
            else bool(record.get("entity_id") or record.get("relation_id"))
        )
        if (
            candidate_id
            and ata
            and anchor_valid
            and category in MAPPING_CATEGORIES
            and category not in {"location_context_ata", "user_declared_ata"}
            and str(record.get("verification_status") or "").lower() == "confirmed"
            and str(record.get("confirmed_claim") or "").strip()
        ):
            result.append(
                {
                    **record,
                    "candidate_id": candidate_id,
                    "ata": ata,
                    "category": category,
                }
            )
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
    for item in validated["document_verification_required"]:
            if (
                item.get("candidate_id") == confirmation.get("candidate_id")
                and item.get("ata") == confirmation.get("ata")
                and item.get("mapping_category") == confirmation.get("category")
                and (not confirmation.get("entity_id") or item.get("entity_id") == confirmation.get("entity_id"))
                and (not confirmation.get("relation_id") or item.get("relation_id") == confirmation.get("relation_id"))
            ):
                matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _action_for_item(
    actions: list[dict[str, object]], category: str, item: dict[str, object]
) -> dict[str, object] | None:
    matches = [
        action
        for action in actions
        if action.get("candidate_id") == item.get("candidate_id")
        and action.get("action") != "add_missing_candidate"
        and action.get("coverage_invalid") is not True
        and action.get("category") == category
        and action.get("ata") == item.get("ata")
    ]
    return matches[0] if len(matches) == 1 else None


def _dedupe_validated(validated: dict[str, list[dict[str, object]]]) -> None:
    for status, items in validated.items():
        seen: set[str] = set()
        unique: list[dict[str, object]] = []
        for item in items:
            key = str(item.get("candidate_id") or "")
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
            elif expected == "array" and isinstance(payload[key], list):
                item_schema = definition.get("items")
                if not isinstance(item_schema, dict):
                    continue
                item_type = item_schema.get("type")
                required = item_schema.get("required", [])
                for index, item in enumerate(payload[key]):
                    if item_type == "object" and not isinstance(item, dict):
                        warnings.append(
                            f"schema_item_type_error:{label}:{key}:{index}:object"
                        )
                        continue
                    if item_type == "string" and not isinstance(item, str):
                        warnings.append(
                            f"schema_item_type_error:{label}:{key}:{index}:string"
                        )
                        continue
                    if isinstance(item, dict):
                        for required_key in required:
                            if required_key not in item:
                                warnings.append(
                                    f"schema_item_missing_required:{label}:{key}:{index}:{required_key}"
                                )
    return warnings


def _fragment_in_request(fragment: str, request: str) -> bool:
    normalized_fragment = " ".join(fragment.lower().split())
    normalized_request = " ".join(request.lower().split())
    return bool(normalized_fragment) and normalized_fragment in normalized_request


def _valid_candidate_id(
    candidate_id: str,
    category: str,
    ata: str,
    entity_id: str,
    relation_id: str,
) -> bool:
    anchor = entity_id or relation_id or "request"
    ata_token = ata.replace(" ", "_").replace("-", "_")
    return bool(
        re.fullmatch(
            rf"candidate:{re.escape(category)}:{re.escape(anchor)}:"
            rf"{re.escape(ata_token)}:[1-9]\d*",
            candidate_id,
        )
    )
