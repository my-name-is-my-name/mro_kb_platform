from __future__ import annotations

import os


PRODUCTION_ATA_MODES = frozenset({"auto", "standard", "extended"})
LEGACY_ATA_MODES = frozenset({"rules_only", "ontology_llm", "full_pipeline"})


def legacy_ata_modes_enabled() -> bool:
    return os.getenv("MRO_KB_ENABLE_LEGACY_ATA_MODES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_ata_runtime_mode(value: object, *, allow_legacy: bool = False) -> str:
    raw = str(value if value is not None else "").strip()
    if not raw:
        raise ValueError("ATA runtime mode must not be empty")
    if raw != raw.lower():
        raise ValueError(f"Invalid ATA runtime mode: {raw}")
    if raw in PRODUCTION_ATA_MODES:
        return raw
    if raw in LEGACY_ATA_MODES:
        if allow_legacy and legacy_ata_modes_enabled():
            return raw
        raise ValueError(f"Legacy ATA runtime mode is disabled: {raw}")
    raise ValueError(f"Invalid ATA runtime mode: {raw}")
