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
        if not catalog_loaded:
            status = "catalog_unavailable"
        elif not matches:
            status = "not_in_certificate"
        elif "-" in ata and exact is None:
            status = "ambiguous_subchapter"
        else:
            status = "in_scope_candidate"
        entry = exact
        if entry is None and matches and "-" not in ata:
            chapter_entry = next(
                (
                    candidate
                    for candidate in matches
                    if normalize_ata(getattr(candidate, "ata", "")) == ata
                ),
                None,
            )
            entry = chapter_entry or (matches[0] if len(matches) == 1 else None)
        result.append(
            {
                "ata": ata,
                "catalog_present": bool(matches),
                "certificate_scope_status": status,
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
