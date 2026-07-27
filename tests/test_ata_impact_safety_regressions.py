from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from core.ata_impact.certificate_validator import validate_certificate
from core.ata_impact.evidence import (
    EvidenceSearchResult,
    LegacyEvidenceRetrieverAdapter,
)
from core.ata_impact.identifiers import extract_identifiers
from core.ata_impact.modes import validate_ata_runtime_mode
from core.ata_impact.prompts import (
    ATA_CRITIC_PROMPT,
    ATA_JSON_REPAIR_PROMPT,
    ATA_MAPPING_AND_CRITIC_PROMPT,
)
from core.ata_impact.service import AtaImpactService
from core.ata_impact.validator import validate_mapping
from core.go_no_go import AtaImpactAgent, CertificateCatalog, CertificateEntry
from tests.test_ata_impact_v2 import SequenceLLM, combined_mapping, confirm_actions, facts


class FixedRetriever:
    def __init__(
        self,
        documents: list[dict[str, object]],
        status: str = "completed",
        warnings: list[str] | None = None,
    ) -> None:
        self.documents = documents
        self.status = status
        self.warnings = warnings or []

    def search(self, **kwargs: object) -> EvidenceSearchResult:
        return EvidenceSearchResult(self.status, self.documents, self.warnings)


class AtaImpactSafetyRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.certificate = CertificateCatalog()

    def run_pipeline(
        self,
        mapping: dict[str, list[dict[str, object]]],
        critic: object,
        *,
        engineering_facts: dict[str, object] | None = None,
        retriever: object | None = None,
        mode: str = "standard",
    ) -> tuple[dict[str, object], SequenceLLM]:
        llm = SequenceLLM(engineering_facts or facts(), mapping, critic)
        result = AtaImpactService(self.certificate, llm, retriever).analyze(
            "A320 equipment corrosion near structure",
            runtime_mode=mode,
        )
        return result, llm

    def test_standard_and_auto_always_use_independent_critic(self) -> None:
        for mode in ("standard", "auto"):
            mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
            result, llm = self.run_pipeline(mapping, confirm_actions(mapping), mode=mode)
            self.assertGreaterEqual(len(llm.calls), 3)
            self.assertEqual(llm.calls[2][0], ATA_CRITIC_PROMPT)
            self.assertNotIn(ATA_MAPPING_AND_CRITIC_PROMPT, [call[0] for call in llm.calls])
            self.assertEqual(
                [item["step"] for item in result["agent_trace"][1:5]],
                [
                    "engineering_fact_extraction",
                    "ata_mapping",
                    "certificate_scope_validation",
                    "independent_critic",
                ],
            )

    def test_missing_empty_duplicate_unknown_and_mismatched_actions_fail_closed(self) -> None:
        cases: list[dict[str, object]] = [
            {"actions": []},
            {"actions": [
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "one",
                },
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "duplicate",
                },
            ]},
            {"actions": [
                {
                    "candidate_id": "candidate:unknown:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "unknown target",
                }
            ]},
            {"actions": [
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "ata": "ATA 53",
                    "category": "structural_ata",
                    "reason": "mutated identity",
                }
            ]},
        ]
        for critic in cases:
            with self.subTest(critic=critic):
                mapping = combined_mapping("ATA 25", context_ata="ATA 25", interface_ata=None)["ata_mapping"]
                mapping["location_context_ata"] = []
                result, _ = self.run_pipeline(mapping, critic)
                self.assertEqual(result["affected_ata"], [])
                self.assertTrue(result["validated_ata"]["candidate_unverified"])
                self.assertEqual(result["decision"], "engineering_review_required")

    def test_critic_unavailable_or_invalid_never_creates_affected(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(facts(), mapping, RuntimeError("offline"))
        result = AtaImpactService(self.certificate, llm).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_same_ata_candidates_keep_unique_identity_and_exact_coverage(self) -> None:
        engineering_facts = facts()
        engineering_facts["physical_objects"].append(  # type: ignore[union-attr]
            {
                "id": "object_2",
                "name": "second damaged item",
                "involvement": "damaged",
                "damage_confirmed": True,
            }
        )
        engineering_facts["damage"].append(  # type: ignore[union-attr]
            {"type": "corrosion", "affected_entity_id": "object_2"}
        )
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        mapping["object_ata"].extend(
            [
                {
                    "candidate_id": "candidate:object_ata:object_2:ATA_25:1",
                    "ata": "ATA 25",
                    "entity_id": "object_2",
                    "confidence": 0.8,
                    "reason": "second entity",
                },
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:2",
                    "ata": "ATA 25",
                    "entity_id": "object_1",
                    "confidence": 0.7,
                    "reason": "independent inspection basis",
                },
            ]
        )
        actions = confirm_actions(mapping)
        result, _ = self.run_pipeline(mapping, actions, engineering_facts=engineering_facts)
        candidates = result["ata_mapping"]["object_ata"]
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({item["candidate_id"] for item in candidates}), 3)
        self.assertEqual(len(result["validated_ata"]["inferred_from_request"]), 3)

    def test_critic_addition_cannot_reuse_existing_candidate_id(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        object_candidate = mapping["object_ata"][0]
        actions = confirm_actions(mapping)
        actions["actions"].append(
            {
                "candidate_id": object_candidate["candidate_id"],
                "action": "add_missing_candidate",
                "ata": "ATA 53",
                "category": "object_ata",
                "entity_id": "object_1",
                "confidence": 0.9,
                "reason": "collision attempt",
            }
        )
        result, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
        )
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertTrue(
            any(
                "critic_addition_candidate_id_collision" in warning
                for warning in result["warnings"]
            )
        )

    def test_extra_unknown_critic_action_invalidates_coverage_globally(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"].append(
            {
                "candidate_id": "candidate:unknown",
                "action": "confirm",
                "reason": "extra unknown action",
            }
        )
        result, _ = self.run_pipeline(mapping, actions)
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_malformed_and_valid_duplicate_actions_fail_coverage(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        candidate_id = mapping["object_ata"][0]["candidate_id"]
        actions = {
            "actions": [
                {
                    "candidate_id": candidate_id,
                    "action": "reject",
                },
                {
                    "candidate_id": candidate_id,
                    "action": "confirm",
                    "reason": "valid-looking duplicate",
                },
            ]
        }
        result, _ = self.run_pipeline(mapping, actions)
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_declared_candidate_id_is_allocated_without_global_collision(self) -> None:
        engineering_facts = facts()
        colliding = "candidate:user_declared_ata:request:ATA_34:1"
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["object_ata"][0]["candidate_id"] = colliding
        validated, _ = validate_mapping(
            mapping,
            engineering_facts,
            ["ATA 34"],
            "ATA 34",
        )
        ids = [
            str(item["candidate_id"])
            for items in validated.values()
            for item in items
        ]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            validated["user_declared_ata"][0]["candidate_state"],
            "candidate_unverified",
        )

    def test_require_document_is_potential_until_exact_controlled_transition(self) -> None:
        for category in ("object_ata", "structural_ata"):
            with self.subTest(category=category):
                engineering_facts = facts(structure_damage=category == "structural_ata")
                mapping = combined_mapping(
                    "ATA 25",
                    structure_affected=category == "structural_ata",
                    interface_ata=None,
                )["ata_mapping"]
                mapping["location_context_ata"] = []
                actions = confirm_actions(mapping)
                target = mapping[category][0]
                for action in actions["actions"]:
                    if action["candidate_id"] == target["candidate_id"]:
                        action["action"] = "require_document"
                result, _ = self.run_pipeline(
                    mapping,
                    actions,
                    engineering_facts=engineering_facts,
                    retriever=FixedRetriever([]),
                )
                self.assertNotIn(target["ata"], result["affected_ata"])
                self.assertIn(target["ata"], result["potentially_affected_ata"])
                self.assertEqual(
                    result["validated_ata"]["document_verification_required"][0]["candidate_id"],
                    target["candidate_id"],
                )

    def test_document_transition_rejects_wrong_nonapplicable_and_obsolete_records(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        target = mapping["object_ata"][0]
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        base = {
            "document_id": "amm-25",
            "document_type": "AMM",
            "revision": "42",
            "effectivity": "A320 all",
            "section_reference": "25-50-00",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {
                    "candidate_id": target["candidate_id"],
                    "ata": target["ata"],
                    "category": "object_ata",
                    "entity_id": "object_1",
                    "verification_status": "confirmed",
                    "confirmed_claim": "the candidate object ATA is applicable",
                }
            ],
        }
        bad_documents = [
            {**base, "applicable": False},
            {**base, "current_revision": False},
            {
                **base,
                "document_id": " ",
                "document_type": " ",
                "revision": " ",
                "effectivity": " ",
                "section_reference": " ",
            },
            {
                **base,
                "confirmed_candidates": [
                    {
                        **base["confirmed_candidates"][0],  # type: ignore[index]
                        "candidate_id": "candidate:wrong",
                    }
                ],
            },
        ]
        for document in bad_documents:
            result, _ = self.run_pipeline(
                mapping,
                actions,
                retriever=FixedRetriever([document]),
            )
            self.assertEqual(result["validated_ata"]["document_confirmed"], [])
            self.assertTrue(result["validated_ata"]["document_verification_required"])
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([base]),
        )
        self.assertEqual(result["validated_ata"]["document_verification_required"], [])
        self.assertEqual(
            result["validated_ata"]["document_confirmed"][0]["previous_status"],
            "document_verification_required",
        )
        self.assertEqual(result["controlled_evidence"][0]["document_id"], "amm-25")

    def test_possible_interface_document_confirmation_is_exact_transition(self) -> None:
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        mapping["object_ata"] = []
        mapping["location_context_ata"] = []
        target = mapping["interface_ata_hypotheses"][0]
        actions = confirm_actions(mapping)
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        document = {
            "document_id": "amm-interface",
            "document_type": "AMM",
            "revision": "7",
            "effectivity": "A320",
            "section_reference": "53-00",
            "trust_level": "approved_data",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {
                    "candidate_id": target["candidate_id"],
                    "ata": target["ata"],
                    "category": "interface_ata_hypotheses",
                    "relation_id": "relation_1",
                    "verification_status": "confirmed",
                    "confirmed_claim": "the attachment interface is affected",
                }
            ],
        }
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([document]),
        )
        self.assertEqual(result["validated_ata"]["document_verification_required"], [])
        self.assertEqual(
            result["validated_ata"]["document_confirmed"][0]["candidate_id"],
            target["candidate_id"],
        )
        self.assertIn("ATA 53", result["affected_ata"])

    def test_require_document_cannot_mutate_candidate_anchor(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"].append(  # type: ignore[union-attr]
            {
                "id": "relation_2",
                "source_entity_id": "object_1",
                "target_entity_id": "structure_1",
                "relation": "attached_to",
                "evidence_type": "explicit",
                "confidence": 0.9,
            }
        )
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        target = mapping["interface_ata_hypotheses"][0]
        actions = confirm_actions(mapping)
        for action in actions["actions"]:
            if action["candidate_id"] == target["candidate_id"]:
                action.update(
                    {
                        "action": "require_document",
                        "relation_id": "relation_2",
                    }
                )
        result, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
        )
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertTrue(
            any(
                warning.startswith("critic_candidate_mismatch:")
                for warning in result["warnings"]
            )
        )

    def test_controlled_evidence_requires_record_identity_consistency(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        target = mapping["object_ata"][0]
        actions = confirm_actions(mapping)
        record = {
            "candidate_id": target["candidate_id"],
            "ata": "ATA 99",
            "category": "object_ata",
            "entity_id": "object_1",
            "verification_status": "confirmed",
            "confirmed_claim": "mismatched claim",
        }
        document = {
            "document_id": "bad",
            "document_type": "AMM",
            "revision": "1",
            "effectivity": "A320",
            "section_reference": "99-00",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [record],
        }
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([document]),
        )
        self.assertEqual(result["controlled_evidence"], [])
        self.assertEqual(result["retrieved_documents"], [document])

    def test_production_evidence_adapter_receives_exact_mapping_candidates(self) -> None:
        class CaptureRetriever:
            def __init__(self) -> None:
                self.filters: dict[str, object] = {}

            def retrieve(
                self,
                query: str,
                filters: dict[str, object],
                limit: int,
            ) -> dict[str, object]:
                self.filters = filters
                return {"status": "completed", "documents": [], "warnings": []}

        candidate = {
            "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
            "ata": "ATA 25",
            "mapping_category": "object_ata",
            "entity_id": "object_1",
            "candidate_state": "candidate_unverified",
        }
        retriever = CaptureRetriever()
        result = LegacyEvidenceRetrieverAdapter(retriever).search(
            request="cargo equipment damage",
            engineering_facts=facts(),
            ata_candidates=["ATA 25"],
            mapping_candidates=[candidate],
            aircraft={"family": "A320"},
            document_types=["AMM"],
            limit=12,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(retriever.filters["mapping_candidates"], [candidate])
        self.assertEqual(
            retriever.filters["mapping_candidates"][0]["candidate_id"],  # type: ignore[index]
            candidate["candidate_id"],
        )

    def test_source_fragment_only_document_record_cannot_confirm_procedure(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        procedure = {
            "candidate_id": (
                "candidate:procedure_ata_hypotheses:request:ATA_51:1"
            ),
            "ata": "ATA 51",
            "confidence": 0.7,
            "reason": "procedure may apply",
            "source_fragment": "equipment corrosion",
        }
        mapping["procedure_ata_hypotheses"] = [procedure]
        actions = confirm_actions(mapping)
        actions["actions"][-1]["action"] = "require_document"  # type: ignore[index]
        document = {
            "document_id": "amm-51",
            "document_type": "AMM",
            "revision": "1",
            "effectivity": "A320",
            "section_reference": "51-00",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {
                    "candidate_id": procedure["candidate_id"],
                    "ata": "ATA 51",
                    "category": "procedure_ata_hypotheses",
                    "source_fragment": "equipment corrosion",
                    "verification_status": "confirmed",
                    "confirmed_claim": "procedure applies",
                }
            ],
        }
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([document]),
        )
        self.assertEqual(result["validated_ata"]["document_confirmed"], [])
        self.assertEqual(result["controlled_evidence"], [])
        self.assertEqual(
            [
                item["candidate_id"]
                for item in result["validated_ata"][
                    "document_verification_required"
                ]
            ],
            [procedure["candidate_id"]],
        )

    def test_installed_in_is_context_not_interface_basis(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "installed_in"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        validated, warnings = validate_mapping(mapping, engineering_facts, [], "installed in")
        self.assertEqual(validated["interface_ata_hypotheses"], [])
        self.assertTrue(any("non_interface_relation" in warning for warning in warnings))

    def test_installed_in_pipeline_keeps_location_context_only(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "installed_in"  # type: ignore[index]
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        result, _ = self.run_pipeline(
            mapping,
            confirm_actions(mapping),
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(result["context_ata"], ["ATA 53"])
        self.assertEqual(result["potentially_affected_ata"], [])

    def test_structured_interface_relations_remain_valid(self) -> None:
        for relation_type in ("attached_to", "connected_to", "requires_access_through"):
            with self.subTest(relation_type=relation_type):
                engineering_facts = facts()
                engineering_facts["relations"][0]["relation"] = relation_type  # type: ignore[index]
                mapping = combined_mapping("ATA 25")["ata_mapping"]
                validated, _ = validate_mapping(mapping, engineering_facts, [], relation_type)
                self.assertTrue(validated["interface_ata_hypotheses"])

    def test_adjacent_relation_requires_structured_interface_basis(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "adjacent_to"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        validated, warnings = validate_mapping(mapping, engineering_facts, [], "protect adjacent")
        self.assertEqual(validated["interface_ata_hypotheses"], [])
        self.assertTrue(
            any(
                warning.startswith("adjacent_without_access_or_protection:")
                for warning in warnings
            )
        )

    def test_reasoning_embedded_json_is_not_accepted_without_successful_repair(self) -> None:
        reasoning = 'analysis {"aircraft":{}} final {"aircraft":{}}'
        result = AtaImpactService(self.certificate, SequenceLLM(reasoning, "still invalid")).analyze(
            "equipment damage"
        )
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_empty_and_array_responses_fail_after_one_repair(self) -> None:
        for raw in ("", "[]"):
            with self.subTest(raw=raw):
                llm = SequenceLLM(raw, "repair also invalid")
                result = AtaImpactService(self.certificate, llm).analyze("equipment damage")
                self.assertEqual(result["affected_ata"], [])
                self.assertEqual(len(llm.calls), 2)

    def test_schema_invalid_mapping_uses_one_repair_with_validation_errors(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"][0].pop("reason")
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(llm.calls[2][0], ATA_JSON_REPAIR_PROMPT)
        repair_payload = json.loads(llm.calls[2][1])
        self.assertEqual(repair_payload["stage"], "ata_mapping")
        self.assertTrue(
            any(
                error.startswith("mapping_item_missing_required:")
                for error in repair_payload["validation_errors"]
            )
        )
        self.assertEqual(result["agent_trace"][2]["repair"], "completed")

    def test_mapping_item_missing_ata_uses_one_schema_repair(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"][0].pop("ata")
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(llm.calls[2][0], ATA_JSON_REPAIR_PROMPT)
        repair_payload = json.loads(llm.calls[2][1])
        self.assertTrue(
            any(
                error.startswith("invalid_ata:")
                for error in repair_payload["validation_errors"]
            )
        )

    def test_non_object_mapping_item_uses_schema_repair(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"].append("junk")  # type: ignore[arg-type]
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        repair_payload = json.loads(llm.calls[2][1])
        self.assertTrue(
            any(
                error.startswith("schema_item_type_error:ata_mapping:")
                for error in repair_payload["validation_errors"]
            )
        )

    def test_fenced_json_and_single_repair_are_supported(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        fenced_facts = "```json\n" + json.dumps(facts()) + "\n```"
        llm = SequenceLLM(
            fenced_facts,
            "invalid mapping response",
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(self.certificate, llm).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(result["agent_trace"][2]["repair"], "completed")

    def test_truncated_response_fails_without_partial_json_recovery(self) -> None:
        class TruncatedLLM:
            def chat_response(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"content": '{"aircraft":', "finish_reason": "length"}

        result = AtaImpactService(self.certificate, TruncatedLLM()).analyze("damage")
        fact_trace = result["agent_trace"][1]
        self.assertEqual(fact_trace["status"], "repair_failed")
        self.assertEqual(fact_trace["warning"], "truncated_response")
        self.assertEqual(fact_trace["reason"], "truncated_response")
        self.assertEqual(fact_trace["repair"], "repair_failed")

    def test_completed_decision_requires_fully_closed_analysis(self) -> None:
        closed_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        completed, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=closed_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(completed["decision"], "completed")

        uncertain_facts = dict(closed_facts)
        uncertain_facts["uncertainties"] = ["effectivity is unclear"]
        uncertain, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=uncertain_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(uncertain["decision"], "additional_input_required")

        no_aircraft = dict(closed_facts)
        no_aircraft["aircraft"] = {"family": None, "model": None, "msn": None, "confidence": 0.5}
        required, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=no_aircraft,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(required["decision"], "additional_input_required")

    def test_unverified_user_declared_ata_blocks_completed(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            context_ata="ATA 53",
            interface_ata=None,
            user_ata="ATA 34",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 cargo equipment damage; request also declares ATA 34",
            runtime_mode="standard",
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(
            [item["ata"] for item in result["validated_ata"]["user_declared_unverified"]],
            ["ATA 34"],
        )
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertIn("user_declared_ata_unresolved", result["decision_reasons"])

    def test_completed_is_blocked_by_critical_warning_or_missing_required_document(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        mapping["interface_ata_hypotheses"] = [
            {
                "candidate_id": "candidate:interface_ata_hypotheses:relation_1:ATA_53:1",
                "ata": "ATA 53",
                "relation_id": "missing",
                "confidence": 0.7,
                "reason": "invalid interface",
            }
        ]
        actions = confirm_actions(mapping)
        warned, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(warned["decision"], "engineering_review_required")
        self.assertIn("critical_validation_warning", warned["decision_reasons"])

        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        missing_document, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(missing_document["decision"], "document_verification_required")
        self.assertEqual(missing_document["validated_ata"]["document_confirmed"], [])
        self.assertEqual(
            missing_document["secondary_ata"][0]["status"],
            "document_verification_required",
        )

        evidence_warning, _ = self.run_pipeline(
            mapping,
            confirm_actions(mapping),
            engineering_facts=engineering_facts,
            retriever=FixedRetriever(
                [],
                warnings=["schema_evidence_integrity_failure"],
            ),
        )
        self.assertEqual(evidence_warning["decision"], "engineering_review_required")
        self.assertIn("critical_validation_warning", evidence_warning["decision_reasons"])

    def test_runtime_modes_are_strict_and_legacy_is_feature_flagged(self) -> None:
        for mode in ("auto", "standard", "extended", " standard "):
            expected = mode.strip()
            self.assertEqual(validate_ata_runtime_mode(mode), expected)
        for mode in ("", "standrd", "AUTO", "Standard"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_ata_runtime_mode(mode)
        for legacy in ("rules_only", "ontology_llm", "full_pipeline"):
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    AtaImpactAgent().analyze("damage", mode=legacy)
            with patch.dict(
                "os.environ",
                {
                    "MRO_KB_ENABLE_LEGACY_ATA_MODES": "true",
                    "MRO_KB_ATA_AGENT_LLM_ENABLED": "0",
                },
            ):
                self.assertEqual(
                    AtaImpactAgent().analyze("damage", mode=legacy)["mode"],
                    legacy,
                )

    def test_candidate_id_must_match_actual_anchor_and_ata(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["object_ata"][0]["candidate_id"] = (
            "candidate:object_ata:wrong_entity:ATA_99:1"
        )
        validated, warnings = validate_mapping(
            mapping,
            facts(),
            [],
            "A320 equipment corrosion near structure",
        )
        candidate = validated["object_ata"][0]
        self.assertEqual(
            candidate["candidate_id"],
            "candidate:object_ata:object_1:ATA_25:1",
        )
        self.assertIn(
            "invalid_candidate_id_format:"
            "candidate:object_ata:wrong_entity:ATA_99:1",
            warnings,
        )

    def test_document_reference_formats_and_declared_ata_separation(self) -> None:
        text = (
            "Использовать AMM ATA 34-11, AMM 34-11, SRM 53-10-01, "
            "IPC ATA 25-50, CMM 32-10-15, WDM 24-00, NTM 51-00, ALS ATA 05-10."
        )
        identifiers = extract_identifiers(text)
        self.assertEqual(identifiers["explicit_ata"], [])
        references = identifiers["document_references"]
        self.assertEqual(
            [item["value"] for item in references],
            ["34-11", "34-11", "53-10-01", "25-50", "32-10-15", "24-00", "51-00", "05-10"],
        )
        self.assertEqual(references[0]["ata"], "ATA 34-11")

    def test_certificate_exact_subchapter_and_missing_subchapter(self) -> None:
        class Catalog:
            entries = [
                CertificateEntry("25", "20", "twenty", ""),
                CertificateEntry("25", "10", "ten", ""),
            ]
            by_system = {"25": entries}

        exact = validate_certificate(Catalog(), ["ATA 25-10"])[0]
        self.assertEqual(exact["certificate_scope_status"], "in_scope_candidate")
        self.assertEqual(exact["certificate_entry"]["name"], "ten")
        missing = validate_certificate(Catalog(), ["ATA 25-30"])[0]
        self.assertEqual(missing["certificate_scope_status"], "ambiguous_subchapter")
        self.assertIsNone(missing["certificate_entry"])
        chapter = validate_certificate(Catalog(), ["ATA 25"])[0]
        self.assertEqual(chapter["certificate_scope_status"], "in_scope_candidate")
        self.assertIsNone(chapter["certificate_entry"])

    def test_no_production_scenario_hardcodes_or_combined_prompt_usage(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / "core" / "ata_impact").glob("*.py")
        )
        self.assertNotIn("ATA_MAPPING_AND_CRITIC_PROMPT,", production)
        for phrase in ("roller track", "gear rib", "shock strut", "static pressure port"):
            self.assertNotIn(phrase, production.lower())


if __name__ == "__main__":
    unittest.main()
