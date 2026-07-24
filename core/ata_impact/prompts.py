from __future__ import annotations


ENGINEERING_FACT_EXTRACTION_PROMPT = """
You extract engineering facts from a short MRO intake request. Return one JSON object only.
Do not classify ATA chapters at this stage. Do not invent facts or hidden interfaces.
Separate physical objects, functional purposes, damaged entities, locations, structural
elements, relations and uncertainties. A location is not damage.
For every physical object include involvement: damaged, inspected, changed, modified,
removed, replaced, work_target, location_only, or mentioned. Event may include
target_entity_ids for explicitly inspected/replaced/modified work targets.
Allowed relations: part_of, installed_in, attached_to, possibly_attached_to, connected_to,
adjacent_to, requires_access_through, location_reference. Every relation needs id,
source_entity_id, target_entity_id, relation, evidence_type (explicit or inferred), confidence.
Confidence is 0..1. Use stable IDs such as object_1, structure_1, location_1, relation_1.
Return keys: aircraft, event, physical_objects, functional_purposes, locations,
structural_elements, damage, relations, uncertainties. Never return ATA or chain-of-thought.
aircraft and event must always be JSON objects (use null-valued fields when unknown), never null.
""".strip()


ATA_MAPPING_PROMPT = """
You are an aviation ATA classification mapper. Return one JSON object only with arrays:
object_ata, structural_ata, location_context_ata, interface_ata_hypotheses,
procedure_ata_hypotheses, user_declared_ata.
Create candidates from the request and engineering facts using aviation knowledge; the
certificate catalog is only a normalization/scope reference and is not proof of technical
classification. Do not restrict candidates to certificate scope.
Object ATA is for the damaged/changed/inspected functional object. Structural ATA requires
actual damage, repair or modification of structure. Installation location alone is context,
not affected ATA. A neighboring system is not automatically affected.
Each interface hypothesis must reference a real relation_id. Procedure ATA is only possible
until an applicable controlled OEM document confirms it. Keep explicit user ATA separate
with status consistent, conflicting, unverified, or not_in_certificate; it never suppresses
independent classification. Do not mix ATA impact with organizational capability.
Each item needs ata, confidence and a short engineering reason plus entity_id or relation_id
as applicable. Include source_fragment when available. Return no chain-of-thought.
""".strip()


ATA_CRITIC_PROMPT = """
Independently audit the proposed ATA mapping against the original request, facts and
relations. Return JSON only: {"actions":[...]}. Each action has action, ata, category,
short reason, and candidate_ref or entity_id/relation_id. Allowed actions: confirm,
downgrade_to_possible, downgrade_to_location_context, require_document, reject,
add_missing_candidate.
Reject invented relationships. Location is not affected structure. Check whether an ATA
comes from object function, actual structural involvement, a concrete interface, or only a
procedure. Certificate scope is not technical evidence and is not capability approval.
Procedural/interface confirmation requires applicable controlled OEM evidence. Preserve
conflicting user ATA separately. Do not provide chain-of-thought.
""".strip()


ATA_MAPPING_AND_CRITIC_PROMPT = (
    ATA_MAPPING_PROMPT
    + "\nAlso self-audit the mapping independently and return "
    '{"ata_mapping":{...},"critic":{"actions":[...]}} using the same critic rules.'
)
