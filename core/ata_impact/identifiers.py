from __future__ import annotations

import re


EXPLICIT_ATA_RE = re.compile(r"\bATA\s*[-:]?\s*(\d{2})(?:\s*[-:]\s*(\d{2}))?\b", re.I)
DOCUMENT_REFERENCE_RE = re.compile(
    r"\b(AMM|SRM|IPC|CMM|WDM|NTM|ALS)\s+(?:ATA\s+)?"
    r"(\d{2}(?:-\d{2}){1,2})\b",
    re.I,
)
AD_RE = re.compile(r"\bAD\s+[A-Z0-9][A-Z0-9./_-]*", re.I)
SB_RE = re.compile(r"\bSB\s+[A-Z0-9][A-Z0-9./_-]*", re.I)
AMOC_RE = re.compile(r"\bAMOC\s+[A-Z0-9][A-Z0-9./_-]*", re.I)
PART_RE = re.compile(r"\bP/?N\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]*)", re.I)
MSN_RE = re.compile(r"\bMSN\s*[:#-]?\s*([A-Z0-9_-]+)", re.I)
AIRCRAFT_RE = re.compile(
    r"\b(?:Airbus\s+)?A(?:irbus\s*)?[- ]?\d{3}(?:-\d{2,3})?"
    r"|\b(?:Boeing\s+)?B?7\d{2}(?:-\d{2,3})?\b",
    re.I,
)


def normalize_ata(value: object) -> str:
    match = re.fullmatch(r"\s*(?:ATA\s*)?(\d{2})(?:\s*[-:]\s*(\d{2}))?\s*", str(value or ""), re.I)
    if not match:
        return ""
    return f"ATA {match.group(1)}" + (f"-{match.group(2)}" if match.group(2) else "")


def extract_identifiers(text: str, fields: dict[str, object] | None = None) -> dict[str, object]:
    fields = fields or {}
    combined = " ".join(
        [text, *[str(fields.get(key) or "") for key in ("ata", "ata_code", "ata_codes", "part_number", "msn", "aircraft_type")]]
    )
    explicit_ata: list[str] = []
    for match in EXPLICIT_ATA_RE.finditer(combined):
        prefix = combined[max(0, match.start() - 12) : match.start()]
        if re.search(r"\b(?:AMM|SRM|IPC|CMM|WDM|NTM|ALS)\s*$", prefix, re.I):
            continue
        ata = normalize_ata(match.group(0))
        if ata and ata not in explicit_ata:
            explicit_ata.append(ata)
    documents = []
    for match in DOCUMENT_REFERENCE_RE.finditer(combined):
        value = match.group(2)
        documents.append(
            {
                "type": match.group(1).upper(),
                "reference": match.group(0),
                "value": value,
                "ata": f"ATA {value}",
            }
        )
    aircraft_match = next(
        (
            match
            for match in AIRCRAFT_RE.finditer(combined)
            if not re.search(r"\b(?:SB|AD)\s*$", combined[max(0, match.start() - 5) : match.start()], re.I)
        ),
        None,
    )
    aircraft = str(fields.get("aircraft_type") or "").strip()
    return {
        "explicit_ata": explicit_ata,
        "ad_references": _matches(AD_RE, combined),
        "sb_references": _matches(SB_RE, combined),
        "amoc_references": _matches(AMOC_RE, combined),
        "part_numbers": _groups(PART_RE, combined),
        "msn": str(fields.get("msn") or "").strip() or _first_group(MSN_RE, combined),
        "aircraft_type_raw": aircraft or (aircraft_match.group(0) if aircraft_match else None),
        "document_references": documents,
    }


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in pattern.finditer(text)))


def _groups(pattern: re.Pattern[str], text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in pattern.finditer(text)))


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None
