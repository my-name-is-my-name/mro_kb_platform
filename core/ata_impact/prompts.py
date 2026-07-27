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
For adjacent_to, include structured interface_basis only when the request explicitly requires
access through or protection of the adjacent item; use access_required or protection_required.
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
Each interface hypothesis must reference a real engineering relation_id. adjacent_to is not
an interface unless its relation has explicit structured interface_basis for access/protection.
Procedure ATA is only possible
until an applicable controlled OEM document confirms it. Keep explicit user ATA separate
with status consistent, conflicting, unverified, or not_in_certificate; it never suppresses
independent classification. Do not mix ATA impact with organizational capability.
Each item needs a unique candidate_id, ata, confidence and a short engineering reason plus
entity_id or relation_id as applicable. candidate_id format is
candidate:<category>:<entity-or-relation>:<ata>:<sequence>. Include source_fragment when
available. All mapping items are unverified candidates. Return no chain-of-thought.
""".strip()


ATA_CRITIC_PROMPT = """
Independently audit the already deterministically validated ATA mapping against the original
request, facts, relations and certificate validation. This is a fresh context: do not assume
or reproduce mapper reasoning. Return JSON only: {"actions":[...]}. Return exactly one action
for every existing mapping candidate (except user_declared_ata), addressed by its exact
candidate_id. Each action has candidate_id, action and short reason; when ata/category are
included they must exactly match the candidate. Allowed actions: confirm,
downgrade_to_possible, downgrade_to_location_context, require_document, reject,
add_missing_candidate.
Reject invented relationships. Location is not affected structure. Check whether an ATA
comes from object function, actual structural involvement, a concrete interface, or only a
procedure. Certificate scope is not technical evidence and is not capability approval.
Procedural/interface confirmation requires applicable controlled OEM evidence. Preserve
conflicting user ATA separately. Do not provide chain-of-thought.
""".strip()


# Deprecated experimental-only contract. Production modes never call this prompt.
ATA_MAPPING_AND_CRITIC_PROMPT = (
    ATA_MAPPING_PROMPT
    + "\nAlso self-audit the mapping independently and return "
    '{"ata_mapping":{...},"critic":{"actions":[...]}} using the same critic rules.'
)


ATA_JSON_REPAIR_PROMPT = """
Repair an invalid structured response for the named ATA pipeline stage. Return exactly one
JSON object and no prose or markdown. Use the supplied validation errors and original stage
contract. Do not add engineering facts that were not present in the supplied input.
""".strip()
