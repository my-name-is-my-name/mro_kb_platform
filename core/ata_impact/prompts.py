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
Represent one real item with one entity ID in exactly one primary role. Do not duplicate the
same named damaged structural item in both physical_objects and structural_elements. A
damaged panel, beam, rib, stringer or other structural item belongs in structural_elements;
nearby coordinates and members remain location/context entities.
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
organization certificate is checked later and must not constrain technical classification.
Use ata_reference as the controlled initial classification vocabulary. chapter_index and
relevant_definitions contain a small retrieved subset of ГОСТ descriptions.
The reference is classification grounding, not OEM evidence and not capability approval.
Object ATA is for the damaged/changed/inspected functional object. Structural ATA requires
actual damage, repair or modification of structure. Installation location alone is context,
not affected ATA. A neighboring system is not automatically affected.
Choose an object's ATA from its stated function and purpose, not from its shape, generic
name, or nearby structural identifier. Respect inclusion and exclusion notes in the supplied
reference. Do not infer that an object is structural unless the facts identify actual
structural involvement.
When the reference explicitly excludes the damaged construction from a functional ATA,
map the actual damaged construction under structural_ata. If facts placed that same damaged
entity in physical_objects, structural_ata may retain its entity_id only with
technical_role=actual_structure. Never use this cross-role correction for a location-only
or merely adjacent entity.
Structural skins, panels, ribs, spars, beams and stringers that form an airframe tank,
wing box, center wing tank, fuselage bay or similar load-bearing cavity remain structural
ATA, not ATA 51 and not the functional system chapter, unless the damaged item is an
actual functional component such as a pump, valve, line, sensor, fitting hardware unit or
other system equipment.
Each interface hypothesis must reference a real engineering relation_id. adjacent_to is not
an interface unless its relation has explicit structured interface_basis for access/protection.
Procedure ATA is only possible
until an applicable controlled OEM document confirms it. A generic inspection, corrosion,
repair, or maintenance action alone is not a procedure ATA anchor. Keep explicit user ATA
separate; Python reconciles its status after independent classification. It never suppresses
independent classification. Do not mix ATA impact with organizational capability.
Each item needs ata, confidence and a short engineering reason plus entity_id or relation_id
as applicable. Do not generate candidate_id; the Python orchestration layer assigns it after
deterministic validation. Include source_fragment when available. Do not return basis,
condition, status, classification_reference_ids, or any other fields. All mapping items are
unverified candidates. Before returning, account for every entity referenced by damage:
each damaged entity needs an object or structural candidate based on its actual function or
structural role. Do not silently omit one damaged object because another object is also
damaged. Copy every entity_id from required_affected_entities into at least one object_ata
or structural_ata item. Preserve its role unless the explicit actual_structure correction
above applies. The number of distinct covered IDs must equal required_affected_entity_count.
Keep each reason under 500 characters and state only
the decisive engineering basis. Return no chain-of-thought.
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
Copy every ID from required_candidate_ids exactly once into actions. The number of ordinary
actions must equal required_candidate_count. A location_context_ata candidate still requires
an action: use confirm when its context role is correct, downgrade_to_location_context when
that is the clearer expression, or reject when unsupported. Never omit a candidate merely
because it is context rather than affected.
Reject invented relationships. Location is not affected structure. Check whether an ATA
comes from object function, actual structural involvement, a concrete interface, or only a
procedure. Certificate scope is not technical evidence and is not capability approval.
Procedural/interface confirmation requires applicable controlled OEM evidence. Preserve
conflicting user ATA separately. Independently cross-check every damaged or changed entity
against the mapping. If a technically affected entity has no candidate, use
add_missing_candidate with a proposal candidate_id, ATA, category and exact entity_id;
Python validates the addition and assigns its final candidate_id. Use the
supplied ata_reference to verify system meaning, but do not treat it as OEM evidence. The
separate certificate validation is scope context only: do not suppress a technically correct
ATA merely because it is absent from the certificate. Do not provide chain-of-thought.
""".strip()


ATA_JSON_REPAIR_PROMPT = """
Repair an invalid structured response for the named ATA pipeline stage. Return exactly one
JSON object and no prose or markdown. Use the supplied validation errors and original stage
contract. Do not add engineering facts that were not present in the supplied input.
""".strip()
