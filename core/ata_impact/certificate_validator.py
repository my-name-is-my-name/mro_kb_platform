from __future__ import annotations

from .identifiers import normalize_ata


def validate_certificate(certificate: object, ata_codes: list[str]) -> list[dict[str, object]]:
    entries = getattr(certificate, "by_system", {})
    catalog_loaded = bool(getattr(certificate, "entries", []))
    result: list[dict[str, object]] = []
    for raw in ata_codes:
        ata = normalize_ata(raw)
        chapter = ata[4:6] if ata else ""
        matches = entries.get(chapter, []) if isinstance(entries, dict) else []
        exact = next(
            (entry for entry in matches if normalize_ata(getattr(entry, "ata", "")) == ata),
            None,
        )
        chapter_entry = next(
            (
                entry
                for entry in matches
                if normalize_ata(getattr(entry, "ata", "")) == f"ATA {chapter}"
            ),
            None,
        )
        if not catalog_loaded:
            status = "catalog_unavailable"
            match_type = None
        elif not matches:
            status = "not_in_certificate"
            match_type = None
        elif exact is not None:
            status = "in_scope_candidate"
            match_type = "exact"
        elif "-" in ata and chapter_entry is not None:
            status = "in_scope_candidate"
            match_type = "chapter"
        elif "-" in ata:
            status = "ambiguous_subchapter"
            match_type = None
        else:
            status = "in_scope_candidate"
            match_type = "chapter"
        entry = exact or chapter_entry
        if entry is None and matches and "-" not in ata:
            entry = chapter_entry or (matches[0] if len(matches) == 1 else None)
        result.append(
            {
                "ata": ata,
                "catalog_present": bool(matches),
                "certificate_scope_status": status,
                "match_type": match_type,
                "certificate_ata": (
                    normalize_ata(getattr(entry, "ata", "")) if entry else None
                ),
                "certificate_entry": (
                    {
                        "name": str(getattr(entry, "name", "")),
                        "description": str(getattr(entry, "explanation", "")),
                    }
                    if entry
                    else None
                ),
            }
        )
    return result


def assess_certificate(
    certificate: object,
    affected_ata: list[str],
    potentially_affected_ata: list[str],
) -> dict[str, object]:
    """Summarize certificate coverage without turning it into capability approval."""

    affected = sorted(
        {ata for value in affected_ata if (ata := normalize_ata(value))}
    )
    potential = sorted(
        {
            ata
            for value in potentially_affected_ata
            if (ata := normalize_ata(value)) and ata not in affected
        }
    )
    checked = [*affected, *potential]
    validation = validate_certificate(certificate, checked)
    catalog_loaded = bool(getattr(certificate, "entries", []))
    covered = [
        item
        for item in validation
        if item["certificate_scope_status"] == "in_scope_candidate"
    ]
    missing = [
        item
        for item in validation
        if item["certificate_scope_status"] != "in_scope_candidate"
    ]
    affected_validation = [
        item for item in validation if item["ata"] in affected
    ]
    affected_covered = [
        item
        for item in affected_validation
        if item["certificate_scope_status"] == "in_scope_candidate"
    ]
    affected_missing = [
        item
        for item in affected_validation
        if item["certificate_scope_status"] != "in_scope_candidate"
    ]
    if not catalog_loaded:
        status = "catalog_unavailable"
    elif not affected:
        status = "undetermined"
    elif affected_missing and not affected_covered:
        status = "not_covered"
    elif not missing:
        status = "covered"
    elif affected_covered:
        status = "partially_covered"
    else:
        status = "not_covered"
    return {
        "status": status,
        "catalog_loaded": catalog_loaded,
        "source": str(getattr(certificate, "path", "")),
        "checked_ata": checked,
        "affected_ata": affected,
        "potentially_affected_ata": potential,
        "matches": covered,
        "missing": missing,
        "capability_approval": False,
    }
