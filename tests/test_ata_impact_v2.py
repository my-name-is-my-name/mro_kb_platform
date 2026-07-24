from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from core.ata_impact.evidence import EvidenceSearchResult, NullAtaEvidenceRetriever
from core.ata_impact.identifiers import extract_identifiers
from core.ata_impact.service import AtaImpactService
from core.go_no_go import CertificateCatalog


class SequenceLLM:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system_prompt: str, user_prompt: str, allow_reasoning_fallback: bool = False) -> str:
        self.calls.append((system_prompt, user_prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


def facts(
    *,
    object_name: str = "roller track guide beam",
    purpose: str = "cargo or baggage handling equipment",
    structure_damage: bool = False,
) -> dict[str, object]:
    return {
        "aircraft": {"family": "A320", "model": None, "msn": None, "confidence": 0.9},
        "event": {"type": "corrosion", "maintenance_action": "inspection_finding"},
        "physical_objects": [{"id": "object_1", "name": object_name, "original_text": object_name, "damage_confirmed": True}],
        "functional_purposes": [{"object_id": "object_1", "description": purpose, "confidence": 0.9}],
        "locations": [{"id": "location_1", "description": "near frame 58", "role": "location_reference"}],
        "structural_elements": [
            {"id": "structure_1", "name": "frame 58", "involvement": "damaged" if structure_damage else "location_reference", "damage_confirmed": structure_damage}
        ],
        "damage": [{"type": "corrosion", "affected_entity_id": "object_1"}],
        "relations": [
            {
                "id": "relation_1",
                "source_entity_id": "object_1",
                "relation": "attached_to" if structure_damage else "possibly_attached_to",
                "target_entity_id": "structure_1",
                "evidence_type": "explicit" if structure_damage else "inferred",
                "confidence": 0.9 if structure_damage else 0.55,
            }
        ],
        "uncertainties": [] if structure_damage else ["Structural attachment involvement is unknown"],
    }


def combined_mapping(
    object_ata: str,
    *,
    structure_affected: bool = False,
    context_ata: str = "ATA 53",
    interface_ata: str | None = "ATA 53",
    user_ata: str | None = None,
    user_status: str = "unverified",
) -> dict[str, object]:
    mapping: dict[str, list[dict[str, object]]] = {
        "object_ata": [{"ata": object_ata, "entity_id": "object_1", "confidence": 0.9, "reason": "functional object", "basis": ["physical_object", "functional_purpose"]}],
        "structural_ata": (
            [{"ata": context_ata, "entity_id": "structure_1", "confidence": 0.9, "reason": "structure is explicitly damaged"}]
            if structure_affected
            else []
        ),
        "location_context_ata": (
            [] if structure_affected else [{"ata": context_ata, "entity_id": "structure_1", "confidence": 0.8, "reason": "location reference", "status": "context_only"}]
        ),
        "interface_ata_hypotheses": (
            [{"ata": interface_ata, "relation_id": "relation_1", "confidence": 0.55, "reason": "attachment may be involved", "condition": "if attachment is damaged"}]
            if interface_ata
            else []
        ),
        "procedure_ata_hypotheses": [],
        "user_declared_ata": (
            [{"ata": user_ata, "confidence": 0.9, "reason": "declared ATA conflicts with object", "status": user_status}]
            if user_ata
            else []
        ),
    }
    return {"ata_mapping": mapping, "critic": {"actions": []}}


class AtaImpactV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.certificate = CertificateCatalog()

    def analyze(self, request: str, engineering_facts: dict[str, object], mapping: dict[str, object], fields: dict[str, object] | None = None) -> dict[str, object]:
        llm = SequenceLLM(engineering_facts, mapping)
        result = AtaImpactService(self.certificate, llm).analyze(request, fields, runtime_mode="standard")
        self.assertEqual(len(llm.calls), 2)
        self.assertNotIn("ATA", json.loads(llm.calls[0][1])["identifiers"].get("aircraft_type_raw") or "")
        return result

    def test_roller_track_location_is_not_affected_structure(self) -> None:
        result = self.analyze(
            "В процессе ТО около FR58 обнаружена коррозия направляющей балки системы роликов заднего багажного отсека",
            facts(),
            combined_mapping("ATA 25"),
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(result["context_ata"], ["ATA 53"])
        self.assertEqual(result["potentially_affected_ata"], ["ATA 53"])
        self.assertEqual(result["validated_ata"]["location_context"][0]["mapping_category"], "location_context_ata")
        self.assertFalse(result["provenance"]["legacy_ontology"] != "disabled")

    def test_location_vs_structural_damage_contrast(self) -> None:
        location = self.analyze("Коррозия roller track в районе шпангоута.", facts(), combined_mapping("ATA 25"))
        damaged = self.analyze(
            "Коррозия roller track и его крепления к шпангоуту.",
            facts(structure_damage=True),
            combined_mapping("ATA 25", structure_affected=True, interface_ata=None),
        )
        self.assertNotIn("ATA 53", location["affected_ata"])
        self.assertIn("ATA 53", damaged["affected_ata"])

    def test_shock_strut_does_not_invent_special_process_ata(self) -> None:
        f = facts(object_name="shock strut sliding cylinder rod", purpose="landing gear shock absorption")
        result = self.analyze("Повреждение хромового покрытия штока подвижного цилиндра основной опоры", f, combined_mapping("ATA 32", context_ata="ATA 32", interface_ata=None))
        self.assertEqual(result["affected_ata"], ["ATA 32"])
        self.assertNotIn("ATA 20", result["affected_ata"] + result["potentially_affected_ata"])
        self.assertNotIn("ATA 51", result["affected_ata"] + result["potentially_affected_ata"])

    def test_gear_rib_interface_is_possible_only(self) -> None:
        f = facts(object_name="Gear Rib 5 lower flange fastener", purpose="wing load-bearing structure")
        mapping = combined_mapping("ATA 57", context_ata="ATA 32", interface_ata="ATA 32")
        result = self.analyze("Коррозия крепежа нижнего фланца Gear Rib 5 у основной опоры", f, mapping)
        self.assertEqual(result["affected_ata"], ["ATA 57"])
        self.assertEqual(result["potentially_affected_ata"], ["ATA 32"])

    def test_static_port_proximity_vs_removal(self) -> None:
        f = facts(object_name="fuselage skin", purpose="fuselage pressure shell")
        near = self.analyze("Царапины обшивки рядом с приёмником статического давления", f, combined_mapping("ATA 53", context_ata="ATA 34", interface_ata="ATA 34"))
        touched = self.analyze(
            "Царапины затрагивают установочную поверхность приёмника; требуется демонтаж",
            facts(object_name="static pressure port mounting surface", purpose="air data sensing", structure_damage=True),
            combined_mapping("ATA 34", structure_affected=False, context_ata="ATA 53", interface_ata=None),
        )
        self.assertNotIn("ATA 34", near["affected_ata"])
        self.assertIn("ATA 34", near["context_ata"] + near["potentially_affected_ata"])
        self.assertIn("ATA 34", touched["affected_ata"])

    def test_conflicting_user_ata_is_preserved_but_not_affected(self) -> None:
        result = self.analyze(
            "Оборудование салона повреждено. Пользователь указал ATA 34",
            facts(object_name="cabin equipment", purpose="passenger cabin furnishing"),
            combined_mapping("ATA 25", context_ata="ATA 53", interface_ata=None, user_ata="ATA 34", user_status="conflicting"),
        )
        self.assertIn("ATA 25", result["affected_ata"])
        self.assertNotIn("ATA 34", result["affected_ata"])
        self.assertEqual(result["ata_mapping"]["user_declared_ata"][0]["status"], "conflicting")

    def test_llm_unavailable_is_explicit_only_fallback(self) -> None:
        result = AtaImpactService(self.certificate, None).analyze("Повреждение неизвестного объекта ATA 34", runtime_mode="standard")
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual([item["ata"] for item in result["validated_ata"]["user_declared_unverified"]], ["ATA 34"])
        self.assertEqual(result["agent_trace"][-1]["mechanism"], "explicit_ata_only")

    def test_invalid_llm_json_fails_safe(self) -> None:
        result = AtaImpactService(self.certificate, SequenceLLM("not-json")).analyze("Повреждение оборудования")
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertEqual(result["affected_ata"], [])
        self.assertIn("llm_unavailable_or_invalid", result["warnings"])

    def test_invalid_interface_relation_is_removed(self) -> None:
        bad = combined_mapping("ATA 25")
        bad["ata_mapping"]["interface_ata_hypotheses"][0]["relation_id"] = "missing_relation"  # type: ignore[index]
        result = self.analyze("Повреждение оборудования", facts(), bad)
        self.assertEqual(result["potentially_affected_ata"], [])
        self.assertTrue(any("interface_without_valid_relation" in warning for warning in result["warnings"]))

    def test_location_relation_cannot_anchor_interface(self) -> None:
        f = facts()
        f["relations"][0]["relation"] = "location_reference"  # type: ignore[index]
        result = self.analyze("Повреждение оборудования около шпангоута", f, combined_mapping("ATA 25"))
        self.assertEqual(result["potentially_affected_ata"], [])
        self.assertTrue(any("non_interface_relation" in warning for warning in result["warnings"]))

    def test_unaffected_neighbor_object_cannot_become_affected(self) -> None:
        f = facts()
        f["physical_objects"].append({"id": "object_2", "name": "neighbor system", "damage_confirmed": False})  # type: ignore[union-attr]
        mapping = combined_mapping("ATA 25")
        mapping["ata_mapping"]["object_ata"].append(  # type: ignore[index]
            {"ata": "ATA 34", "entity_id": "object_2", "confidence": 0.9, "reason": "nearby object"}
        )
        result = self.analyze("Повреждение объекта рядом с соседней системой", f, mapping)
        self.assertNotIn("ATA 34", result["affected_ata"])

    def test_duplicate_entity_id_fails_safe(self) -> None:
        f = facts()
        f["physical_objects"].append({"id": "structure_1", "name": "duplicate", "damage_confirmed": True})  # type: ignore[union-attr]
        result = AtaImpactService(self.certificate, SequenceLLM(f)).analyze("Повреждение около location structure", runtime_mode="standard")
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertNotIn("ATA 53", result["affected_ata"])

    def test_procedure_requires_factual_anchor(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)
        mapping["ata_mapping"]["procedure_ata_hypotheses"] = [  # type: ignore[index]
            {"ata": "ATA 34", "confidence": 0.7, "reason": "possible test"}
        ]
        result = self.analyze("Повреждение оборудования", facts(), mapping)
        self.assertEqual(result["validated_ata"]["possible_procedure"], [])
        self.assertTrue(any("procedure_without_factual_anchor" in warning for warning in result["warnings"]))

        mapping = combined_mapping("ATA 25", interface_ata=None)
        mapping["ata_mapping"]["procedure_ata_hypotheses"] = [  # type: ignore[index]
            {"ata": "ATA 34", "confidence": 0.7, "reason": "possible test", "source_fragment": "invented fragment"}
        ]
        result = self.analyze("Повреждение оборудования", facts(), mapping)
        self.assertEqual(result["validated_ata"]["possible_procedure"], [])

    def test_critic_can_downgrade_structural_candidate_to_location(self) -> None:
        f = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", structure_affected=True, interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            f,
            mapping,
            {"actions": [{"action": "downgrade_to_location_context", "ata": "ATA 53", "category": "structural_ata", "entity_id": "structure_1", "reason": "request only uses frame as location"}]},
        )
        result = AtaImpactService(self.certificate, llm).analyze("AD 2024-01 equipment near frame", runtime_mode="extended")
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertIn("ATA 53", result["context_ata"])

    def test_critic_can_downgrade_object_through_its_real_interface(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            mapping,
            {"actions": [{"action": "downgrade_to_possible", "ata": "ATA 25", "category": "object_ata", "entity_id": "object_1", "relation_id": "relation_1", "reason": "object involvement is conditional"}]},
        )
        result = AtaImpactService(self.certificate, llm).analyze("AD 2024-01 equipment near attachment", runtime_mode="extended")
        self.assertNotIn("ATA 25", result["affected_ata"])
        self.assertIn("ATA 25", result["potentially_affected_ata"])
        self.assertEqual(result["validated_ata"]["possible_interface"][0]["relation_id"], "relation_1")

    def test_adjacent_protection_can_be_possible_interface(self) -> None:
        f = facts()
        f["relations"][0]["relation"] = "adjacent_to"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")
        mapping["ata_mapping"]["interface_ata_hypotheses"][0]["condition"] = "protect adjacent equipment during access"  # type: ignore[index]
        result = self.analyze("Protect adjacent structure during access", f, mapping)
        self.assertIn("ATA 53", result["potentially_affected_ata"])

    def test_auto_escalates_single_engineering_interface(self) -> None:
        llm = SequenceLLM(facts(), combined_mapping("ATA 25")["ata_mapping"], {"actions": []})
        result = AtaImpactService(self.certificate, llm).analyze("Equipment corrosion near attachment", runtime_mode="auto")
        self.assertEqual(result["runtime_mode"], "extended")
        self.assertEqual(len(llm.calls), 3)

    def test_location_only_structure_cannot_be_mapped_as_affected(self) -> None:
        bad = combined_mapping("ATA 25")
        bad["ata_mapping"]["structural_ata"] = [  # type: ignore[index]
            {"ata": "ATA 53", "entity_id": "structure_1", "confidence": 0.99, "reason": "frame was mentioned"}
        ]
        result = self.analyze("Коррозия оборудования около шпангоута", facts(), bad)
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertTrue(any("structural_ata_without_involvement" in warning for warning in result["warnings"]))

    def test_critic_cannot_add_interface_without_relation(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            mapping,
            {"actions": [{"action": "add_missing_candidate", "ata": "ATA 53", "category": "interface_ata_hypotheses", "confidence": 0.8, "reason": "unsupported"}]},
        )
        result = AtaImpactService(self.certificate, llm).analyze("AD 2024-01 equipment damage", runtime_mode="extended")
        self.assertNotIn("ATA 53", result["potentially_affected_ata"])
        self.assertTrue(any("critic_addition_rejected" in warning for warning in result["warnings"]))

    def test_retriever_exception_fails_safe(self) -> None:
        class BrokenRetriever:
            def search(self, **kwargs: object) -> EvidenceSearchResult:
                raise RuntimeError("offline")

        llm = SequenceLLM(facts(), combined_mapping("ATA 25"))
        result = AtaImpactService(self.certificate, llm, BrokenRetriever()).analyze("Повреждение оборудования", runtime_mode="standard")
        self.assertEqual(result["document_verification"]["status"], "error")
        self.assertEqual(result["validated_ata"]["document_confirmed"], [])

    def test_document_confirmation_requires_explicit_complete_verification_record(self) -> None:
        class Retriever:
            def __init__(self, document: dict[str, object]) -> None:
                self.document = document

            def search(self, **kwargs: object) -> EvidenceSearchResult:
                return EvidenceSearchResult("completed", [self.document])

        incomplete = {"document_id": "doc", "ata": "ATA 25", "trust_level": "controlled_oem"}
        llm = SequenceLLM(facts(), combined_mapping("ATA 25"))
        result = AtaImpactService(self.certificate, llm, Retriever(incomplete)).analyze("Повреждение оборудования", runtime_mode="standard")
        self.assertEqual(result["validated_ata"]["document_confirmed"], [])

        complete = {
            "document_id": "doc",
            "document_type": "AMM",
            "revision": "42",
            "effectivity": "A320 all",
            "section_reference": "25-50-00 p.2",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {"ata": "ATA 25", "category": "object_ata", "entity_id": "object_1"}
            ],
        }
        llm = SequenceLLM(facts(), combined_mapping("ATA 25"))
        result = AtaImpactService(self.certificate, llm, Retriever(complete)).analyze("Повреждение оборудования", runtime_mode="standard")
        self.assertEqual([item["ata"] for item in result["validated_ata"]["document_confirmed"]], ["ATA 25"])

        location_confirmation = {
            **complete,
            "confirmed_candidates": [
                {"ata": "ATA 53", "category": "location_context_ata", "entity_id": "structure_1"}
            ],
        }
        llm = SequenceLLM(facts(), combined_mapping("ATA 25"))
        result = AtaImpactService(self.certificate, llm, Retriever(location_confirmation)).analyze("Повреждение оборудования", runtime_mode="standard")
        self.assertNotIn("ATA 53", result["affected_ata"])

    def test_critic_cannot_turn_object_into_interface_without_relation(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            mapping,
            {"actions": [{"action": "downgrade_to_possible", "ata": "ATA 25", "category": "object_ata", "entity_id": "object_1", "reason": "uncertain"}]},
        )
        result = AtaImpactService(self.certificate, llm).analyze("AD 2024-01 equipment damage", runtime_mode="extended")
        self.assertEqual(result["potentially_affected_ata"], [])
        self.assertTrue(any("incompatible_critic_action" in warning for warning in result["warnings"]))

    def test_empty_valid_json_stage_fails_safe(self) -> None:
        result = AtaImpactService(self.certificate, SequenceLLM({})).analyze("Повреждение оборудования")
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertEqual(result["affected_ata"], [])

    def test_metamorphic_msn_and_sentence_order_do_not_change_assembly(self) -> None:
        variants = [
            "Обнаружена коррозия направляющей. Объект расположен в багажном отсеке.",
            "В багажном отсеке расположен объект. Обнаружена коррозия направляющей. MSN 1234.",
        ]
        outputs = [
            self.analyze(request, facts(), combined_mapping("ATA 25"), {"msn": "1234"} if "MSN" in request else None)
            for request in variants
        ]
        self.assertEqual(outputs[0]["affected_ata"], outputs[1]["affected_ata"])
        self.assertEqual(outputs[0]["context_ata"], outputs[1]["context_ata"])

    def test_extended_mode_uses_three_logical_calls(self) -> None:
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        llm = SequenceLLM(facts(), mapping, {"actions": [{"action": "confirm", "ata": "ATA 25", "category": "object_ata", "reason": "supported by request"}]})
        result = AtaImpactService(self.certificate, llm).analyze("AD 2024-01 multiple objects", runtime_mode="extended")
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(result["validated_ata"]["direct_confirmed"][0]["critic_action"], "confirm")

    def test_null_retriever_never_confirms_hypothesis(self) -> None:
        result = self.analyze("Повреждение оборудования", facts(), combined_mapping("ATA 25"))
        self.assertEqual(result["document_verification"]["status"], "not_configured")
        self.assertEqual(result["validated_ata"]["document_confirmed"], [])

    def test_same_ata_is_not_hidden_when_it_has_multiple_roles(self) -> None:
        mapping = combined_mapping("ATA 25", context_ata="ATA 25", interface_ata="ATA 25")
        result = self.analyze("Equipment damage near same-chapter structure", facts(), mapping)
        self.assertIn("ATA 25", result["affected_ata"])
        self.assertIn("ATA 25", result["potentially_affected_ata"])
        self.assertIn("ATA 25", result["context_ata"])

    def test_identifier_extraction_does_not_classify_frame(self) -> None:
        ids = extract_identifiers("A320 MSN 1234, AD 2024-01, SB A320-53-1000, P/N ABC-1 near FR58. AMM 25-50-00")
        self.assertEqual(ids["explicit_ata"], [])
        self.assertEqual(ids["msn"], "1234")
        self.assertEqual(ids["part_numbers"], ["ABC-1"])
        self.assertNotIn("FR58", json.dumps(ids))
        from core.ata_impact.identifiers import normalize_ata
        self.assertEqual(normalize_ata("A320"), "")
        sb_only = extract_identifiers("SB A320-53-1000")
        self.assertIsNone(sb_only["aircraft_type_raw"])

    def test_certificate_missing_is_catalog_unavailable(self) -> None:
        missing = CertificateCatalog(Path("/tmp/definitely-missing-certificate.docx"))
        result = AtaImpactService(missing, None).analyze("Работа ATA 25")
        self.assertEqual(result["certificate_validation"][0]["certificate_scope_status"], "catalog_unavailable")

    def test_production_has_no_test_specific_mapping(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production = "\n".join(path.read_text(encoding="utf-8") for path in (root / "core" / "ata_impact").glob("*.py"))
        for phrase in ("ROLLER TRACK", "frame 58", "shock strut", "Gear Rib", "static pressure port"):
            self.assertNotIn(phrase.lower(), production.lower())


@unittest.skipUnless(os.getenv("MRO_KB_RUN_ATA_LLM_INTEGRATION") == "1", "opt-in real LLM integration")
class RealLLMIntegrationTests(unittest.TestCase):
    def test_repeated_semantic_matrix(self) -> None:
        from core.runtime_clients import OpenAICompatibleLLM, RuntimeSettings

        service = AtaImpactService(CertificateCatalog(), OpenAICompatibleLLM(RuntimeSettings()))
        requests = {
            "Коррозия roller track в районе шпангоута 58.": ({"ATA 25"}, set(), {"ATA 53"}),
            "Corrosion of a roller track guide beam in the aft cargo compartment near FR58.": ({"ATA 25"}, set(), {"ATA 53"}),
            "Повреждение хромового покрытия штока подвижного цилиндра основной опоры.": ({"ATA 32"}, set(), set()),
            "Царапины обшивки рядом с приёмником статического давления.": (set(), set(), {"ATA 34"}),
        }
        observations: dict[str, list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {}
        for request in requests:
            observations[request] = []
            for _ in range(3):
                result = service.analyze(request, runtime_mode="extended")
                observations[request].append(
                    (
                        tuple(result["affected_ata"]),
                        tuple(result["potentially_affected_ata"]),
                        tuple(result["context_ata"]),
                    )
                )
        for request, runs in observations.items():
            self.assertEqual(len(set(runs)), 1, f"unstable classification for {request}: {runs}")
            expected_affected, expected_potential, expected_context = requests[request]
            affected, potential, context = map(set, runs[0])
            self.assertTrue(expected_affected <= affected, (request, runs[0]))
            self.assertTrue(expected_potential <= potential, (request, runs[0]))
            self.assertTrue(expected_context <= context | potential, (request, runs[0]))
            if "roller track" in request.lower():
                self.assertNotIn("ATA 53", affected)
            if "статического" in request:
                self.assertNotIn("ATA 34", affected)


if __name__ == "__main__":
    unittest.main()
