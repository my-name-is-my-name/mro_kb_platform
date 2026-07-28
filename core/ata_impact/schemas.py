"""Portable generation schemas for the three ATA Impact LLM stages."""

from __future__ import annotations


_CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
_NULLABLE_STRING = {"type": ["string", "null"]}

ENGINEERING_FACTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "aircraft",
        "event",
        "physical_objects",
        "functional_purposes",
        "locations",
        "structural_elements",
        "damage",
        "relations",
        "uncertainties",
    ],
    "properties": {
        "aircraft": {
            "type": "object",
            "additionalProperties": False,
            "required": ["family", "model", "msn", "confidence"],
            "properties": {
                "family": _NULLABLE_STRING,
                "model": _NULLABLE_STRING,
                "msn": _NULLABLE_STRING,
                "confidence": _CONFIDENCE,
            },
        },
        "event": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "maintenance_action"],
            "properties": {
                "type": _NULLABLE_STRING,
                "maintenance_action": _NULLABLE_STRING,
                "target_entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "physical_objects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "involvement", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "original_text": {"type": "string"},
                    "involvement": {
                        "type": "string",
                        "enum": [
                            "damaged",
                            "inspected",
                            "changed",
                            "modified",
                            "removed",
                            "replaced",
                            "work_target",
                            "location_only",
                            "mentioned",
                        ],
                    },
                    "damage_confirmed": {"type": "boolean"},
                    "confidence": _CONFIDENCE,
                },
            },
        },
        "functional_purposes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["object_id", "description", "confidence"],
                "properties": {
                    "object_id": {"type": "string"},
                    "description": {"type": "string"},
                    "confidence": _CONFIDENCE,
                },
            },
        },
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "description", "role", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "role": {"type": "string"},
                    "confidence": _CONFIDENCE,
                },
            },
        },
        "structural_elements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "involvement", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "involvement": {"type": "string"},
                    "damage_confirmed": {"type": "boolean"},
                    "confidence": _CONFIDENCE,
                },
            },
        },
        "damage": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "affected_entity_id"],
                "properties": {
                    "type": {"type": "string"},
                    "affected_entity_id": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "source_entity_id",
                    "target_entity_id",
                    "relation",
                    "evidence_type",
                    "confidence",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "source_entity_id": {"type": "string"},
                    "target_entity_id": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": [
                            "part_of",
                            "installed_in",
                            "attached_to",
                            "possibly_attached_to",
                            "connected_to",
                            "adjacent_to",
                            "requires_access_through",
                            "location_reference",
                        ],
                    },
                    "evidence_type": {
                        "type": "string",
                        "enum": ["explicit", "inferred", "user_declared", "document"],
                    },
                    "interface_basis": {
                        "type": "string",
                        "enum": ["access_required", "protection_required"],
                    },
                    "confidence": _CONFIDENCE,
                },
            },
        },
        "uncertainties": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def _mapping_item(*, anchor: str | None, extra: dict[str, object] | None = None) -> dict[str, object]:
    properties: dict[str, object] = {
        "ata": {"type": "string"},
        "confidence": _CONFIDENCE,
        "reason": {"type": "string", "maxLength": 500},
        "source_fragment": {"type": "string", "maxLength": 300},
        # Compatibility-only optional fields. Production prompts do not request
        # them; deterministic Python code owns candidate state and provenance.
        "basis": {"type": "array", "items": {"type": "string"}},
        "condition": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "consistent",
                "conflicting",
                "unverified",
                "not_in_certificate",
                "context_only",
                "hypothesis",
            ],
        },
        "technical_role": {
            "type": "string",
            "enum": [
                "functional_object",
                "actual_structure",
                "location_context",
            ],
        },
    }
    required = ["ata", "confidence", "reason"]
    if anchor:
        properties[anchor] = {"type": "string"}
        required.append(anchor)
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


ATA_MAPPING_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "object_ata",
        "structural_ata",
        "location_context_ata",
        "interface_ata_hypotheses",
        "procedure_ata_hypotheses",
        "user_declared_ata",
    ],
    "properties": {
        "object_ata": {
            "type": "array",
            "items": _mapping_item(anchor="entity_id"),
        },
        "structural_ata": {
            "type": "array",
            "items": _mapping_item(anchor="entity_id"),
        },
        "location_context_ata": {
            "type": "array",
            "items": _mapping_item(anchor="entity_id"),
        },
        "interface_ata_hypotheses": {
            "type": "array",
            "items": _mapping_item(anchor="relation_id"),
        },
        "procedure_ata_hypotheses": {
            "type": "array",
            "items": _mapping_item(
                anchor=None,
                extra={
                    "entity_id": {"type": "string"},
                    "relation_id": {"type": "string"},
                },
            ),
        },
        "user_declared_ata": {
            "type": "array",
            "items": _mapping_item(anchor=None),
        },
    },
}


def _generation_mapping_item(
    *,
    anchor: str | None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    properties: dict[str, object] = {
        "ata": {"type": "string"},
        "confidence": _CONFIDENCE,
        "reason": {"type": "string", "maxLength": 500},
        "source_fragment": {"type": "string", "maxLength": 300},
    }
    required = ["ata", "confidence", "reason"]
    if anchor:
        properties[anchor] = {"type": "string"}
        required.append(anchor)
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


ATA_MAPPING_GENERATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(ATA_MAPPING_SCHEMA["required"]),
    "properties": {
        "object_ata": {
            "type": "array",
            "items": _generation_mapping_item(anchor="entity_id"),
        },
        "structural_ata": {
            "type": "array",
            "items": _generation_mapping_item(
                anchor="entity_id",
                extra={
                    "technical_role": {
                        "type": "string",
                        "enum": ["actual_structure"],
                    }
                },
            ),
        },
        "location_context_ata": {
            "type": "array",
            "items": _generation_mapping_item(anchor="entity_id"),
        },
        "interface_ata_hypotheses": {
            "type": "array",
            "items": _generation_mapping_item(anchor="relation_id"),
        },
        "procedure_ata_hypotheses": {
            "type": "array",
            "items": _generation_mapping_item(
                anchor=None,
                extra={
                    "entity_id": {"type": "string"},
                    "relation_id": {"type": "string"},
                },
            ),
        },
        "user_declared_ata": {
            "type": "array",
            "items": _generation_mapping_item(anchor=None),
        },
    },
}

ATA_CRITIC_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_id", "action", "reason"],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "confirm",
                            "downgrade_to_possible",
                            "downgrade_to_location_context",
                            "require_document",
                            "reject",
                            "add_missing_candidate",
                        ],
                    },
                    "reason": {"type": "string"},
                    "ata": {"type": "string"},
                    "category": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "relation_id": {"type": "string"},
                    "confidence": _CONFIDENCE,
                    "source_fragment": {"type": "string"},
                    # Accepted for compatibility with older critic fixtures;
                    # orchestration does not trust these fields as state.
                    "condition": {"type": "string"},
                    "basis": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string"},
                },
            },
        }
    },
}

ATA_CRITIC_GENERATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_id", "action", "reason"],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "action": ATA_CRITIC_SCHEMA["properties"]["actions"]["items"]["properties"]["action"],  # type: ignore[index]
                    "reason": {"type": "string"},
                    "ata": {"type": "string"},
                    "category": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "relation_id": {"type": "string"},
                },
            },
        }
    },
}
