from __future__ import annotations


ATA_FIELD_NAMES = frozenset(
    {
        "aircraft_type",
        "component",
        "components",
        "asset_name",
        "zone",
        "zones",
        "part_number",
        "ata",
        "ata_code",
        "ata_codes",
    }
)
ATA_REQUEST_ALIASES = ("request", "question", "q")


def extract_ata_request_text(payload: dict[str, object]) -> str:
    supplied = {
        key: str(payload[key]).strip()
        for key in ATA_REQUEST_ALIASES
        if key in payload and str(payload[key]).strip()
    }
    if len(set(supplied.values())) > 1:
        raise ValueError("Conflicting ATA request text aliases")
    return next(
        (supplied[key] for key in ATA_REQUEST_ALIASES if key in supplied),
        "",
    )


def merge_ata_request_fields(payload: dict[str, object]) -> dict[str, object]:
    nested = payload.get("fields")
    fields = dict(nested) if isinstance(nested, dict) else {}
    for key in ATA_FIELD_NAMES:
        if key not in payload:
            continue
        if key in fields and fields[key] != payload[key]:
            raise ValueError(f"Conflicting flat and nested ATA field: {key}")
        fields[key] = payload[key]
    return fields


def validate_stream_flag(payload: dict[str, object]) -> bool:
    if "stream" not in payload:
        return False
    if not isinstance(payload["stream"], bool):
        raise ValueError("stream must be a boolean")
    return payload["stream"]
