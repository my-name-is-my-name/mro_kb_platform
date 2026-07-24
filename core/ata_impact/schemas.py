"""JSON Schema documents used as the public structured-output contracts.

Runtime validation is implemented without a third-party dependency in
``validator.py`` and enforces the same required shapes plus cross-reference
rules that JSON Schema alone cannot express.
"""

ENGINEERING_FACTS_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
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
        "aircraft": {"type": "object"},
        "event": {"type": "object"},
        "physical_objects": {"type": "array", "items": {"type": "object"}},
        "functional_purposes": {"type": "array", "items": {"type": "object"}},
        "locations": {"type": "array", "items": {"type": "object"}},
        "structural_elements": {"type": "array", "items": {"type": "object"}},
        "damage": {"type": "array", "items": {"type": "object"}},
        "relations": {"type": "array", "items": {"type": "object"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
}

ATA_MAPPING_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "object_ata",
        "structural_ata",
        "location_context_ata",
        "interface_ata_hypotheses",
        "procedure_ata_hypotheses",
        "user_declared_ata",
    ],
    "properties": {
        key: {"type": "array", "items": {"type": "object", "required": ["ata", "confidence", "reason"]}}
        for key in (
            "object_ata",
            "structural_ata",
            "location_context_ata",
            "interface_ata_hypotheses",
            "procedure_ata_hypotheses",
            "user_declared_ata",
        )
    },
}

ATA_CRITIC_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {"type": "object", "required": ["action", "ata", "category", "reason"]},
        }
    },
}
