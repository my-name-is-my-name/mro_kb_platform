from __future__ import annotations

from typing import Final


RELATION_TYPES: Final = {
    "part_of",
    "installed_in",
    "attached_to",
    "possibly_attached_to",
    "connected_to",
    "adjacent_to",
    "requires_access_through",
    "location_reference",
}
EVIDENCE_TYPES: Final = {"explicit", "inferred", "user_declared", "document"}
MAPPING_CATEGORIES: Final = (
    "object_ata",
    "structural_ata",
    "location_context_ata",
    "interface_ata_hypotheses",
    "procedure_ata_hypotheses",
    "user_declared_ata",
)
FINAL_STATUSES: Final = (
    "candidate_unverified",
    "direct_confirmed",
    "inferred_from_request",
    "location_context",
    "possible_interface",
    "possible_procedure",
    "document_verification_required",
    "document_confirmed",
    "user_declared_consistent",
    "user_declared_conflicting",
    "user_declared_unverified",
    "user_declared_not_in_certificate",
    "rejected",
)
CRITIC_ACTIONS: Final = {
    "confirm",
    "downgrade_to_possible",
    "downgrade_to_location_context",
    "require_document",
    "reject",
    "add_missing_candidate",
}


def empty_mapping() -> dict[str, list[dict[str, object]]]:
    return {key: [] for key in MAPPING_CATEGORIES}


def empty_validated() -> dict[str, list[dict[str, object]]]:
    return {key: [] for key in FINAL_STATUSES}
