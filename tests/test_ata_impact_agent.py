from __future__ import annotations

import unittest

from core.go_no_go import AtaImpactAgent, CertificateCatalog


class FakeEvidenceRetriever:
    def __init__(self) -> None:
        self.calls = 0

    def retrieve(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {"documents": []}


class FakeReActLLM:
    def __init__(self, response: str) -> None:
        self.response = response

    def chat(self, system: str, user: str, allow_reasoning_fallback: bool = False) -> str:
        return self.response

    def health(self) -> dict[str, object]:
        return {"ok": True, "provider": "fake"}


class AtaImpactAgentTests(unittest.TestCase):
    def test_full_pipeline_confirms_secondary_only_from_applicable_controlled_document(self) -> None:
        class Retriever:
            def retrieve(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"documents": [{
                    "document_id": "a320-srm-51", "title": "A320 SRM ATA 51", "snippet": "ATA 51 approved repair data",
                    "document_type": "SRM", "ata": "ATA 51", "aircraft_type": "A320",
                    "trust_level": "controlled_oem", "source_origin": "internal",
                }]}

        result = AtaImpactAgent(retriever=Retriever()).analyze(
            "Вмятина обшивки фюзеляжа; установить усиливающую накладку", {"aircraft_type": "A320"}, mode="full_pipeline"
        )
        self.assertEqual(result["confirmed_affected_ata"], ["ATA 51"])
        self.assertEqual(result["secondary_ata_hypotheses"][0]["status"], "controlled_evidence_found")
        self.assertEqual(result["agent_trace"][-1]["reason"], "two_pass_limit_reached")

    def test_external_or_historical_material_never_confirms_secondary(self) -> None:
        class Retriever:
            def retrieve(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"documents": [{"document_id": "old-case", "title": "MRO case ATA 51", "snippet": "ATA 51", "trust_level": "internal_reference", "document_type": "SRM", "aircraft_type": "A320"}]}

        class Internet:
            def retrieve(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"status": "completed", "documents": [{"document_id": "easa", "title": "EASA AD", "source_url": "https://ad.easa.europa.eu/example"}]}

        result = AtaImpactAgent(retriever=Retriever(), internet_retriever=Internet()).analyze(
            "Вмятина обшивки фюзеляжа; установить усиливающую накладку", {"aircraft_type": "A320"}, mode="full_pipeline"
        )
        self.assertEqual(result["confirmed_affected_ata"], [])
        self.assertEqual(result["internet_context"][0]["trust_level"], "regulatory_external")
        self.assertIn("engineering_review_or_controlled_document_required", result["warnings"])

    def test_document_list_does_not_match_modification_secondary_term(self) -> None:
        result = AtaImpactAgent().analyze(
            "Требуется перечень применимых документов для ремонта обшивки фюзеляжа",
            {"aircraft_type": "A320"}, mode="rules_only",
        )
        self.assertNotIn("ATA 51", [item["ata"] for item in result["secondary_ata_hypotheses"]])

    def test_ordinary_fuselage_damage_does_not_always_create_ata_51(self) -> None:
        result = AtaImpactAgent().analyze(
            "Вмятина и трещина обшивки фюзеляжа между FR23 и FR24. Требуется оценить ремонт.",
            {"aircraft_type": "A320"}, mode="rules_only",
        )
        self.assertEqual([item["ata"] for item in result["secondary_ata_hypotheses"]], [])

    def test_structural_doubler_creates_ata_51_hypothesis(self) -> None:
        result = AtaImpactAgent().analyze(
            "Установить усиливающую накладку на поврежденной обшивке фюзеляжа.",
            {"aircraft_type": "A320"}, mode="rules_only",
        )
        self.assertEqual([item["ata"] for item in result["secondary_ata_hypotheses"]], ["ATA 51"])

    def test_cargo_roller_track_is_not_inferred_as_ata_25_without_controlled_mapping(self) -> None:
        result = AtaImpactAgent().analyze(
            "В районе шпангоута 58 обнаружена коррозия направляющей балки для системы роликов (ROLLER TRACK) заднего багажного отсека.",
            {"aircraft_type": "A320"}, mode="rules_only",
        )
        self.assertEqual(result["direct_system_ata"], [])
        self.assertEqual([item["ata"] for item in result["direct_structural_ata"]], ["ATA 53"])
        self.assertIn("описанием главы сертификата", result["direct_structural_ata"][0]["reason"])

    def test_static_pressure_is_direct_ata_34_without_document_search(self) -> None:
        retriever = FakeEvidenceRetriever()
        result = AtaImpactAgent(retriever=retriever).analyze(
            "Устранение царапин в районе приемника статического давления",
            {"aircraft_type": "Airbus A320", "component": "приемник статического давления"},
            mode="rules_only",
        )
        self.assertEqual(result["direct_ata"], ["ATA 34"])
        self.assertEqual(result["direct_structural_ata"], [])
        self.assertEqual(result["capability_screening"], "not_assessed")
        self.assertEqual(retriever.calls, 0)
        self.assertIn("ATA 34", result["answer"])

    def test_procedure_reference_never_creates_ata_scope(self) -> None:
        result = AtaImpactAgent().analyze(
            "Выполнить NTM 51-10-08 для контроля зоны",
            {"aircraft_type": "Airbus A320"},
            mode="rules_only",
        )
        self.assertEqual(result["direct_ata"], [])
        self.assertEqual(result["procedure_references"][0]["reference"], "NTM 51-10-08")

    def test_declared_out_of_certificate_ata_is_not_capability(self) -> None:
        result = AtaImpactAgent(CertificateCatalog()).analyze("Работа по ATA 99", mode="rules_only")
        self.assertEqual(result["certificate_chapter_match"]["status"], "out_of_scope")
        self.assertEqual(result["capability_screening"], "not_assessed")

    def test_llm_cannot_invent_ata(self) -> None:
        result = AtaImpactAgent(llm=FakeReActLLM('{"direct_ata":["ATA 99"]}')).analyze(
            "Работа с приемником статического давления",
            mode="ontology_llm",
        )
        self.assertEqual(result["direct_ata"], [])

    def test_nacelle_context_excludes_fuselage_frame_match(self) -> None:
        result = AtaImpactAgent().analyze(
            "Повреждение фланца переднего шпангоута створки реверсивного устройства двигателя",
            {"aircraft_type": "A320"},
            mode="rules_only",
        )
        self.assertEqual(result["direct_ata"], ["ATA 54"])


if __name__ == "__main__":
    unittest.main()
