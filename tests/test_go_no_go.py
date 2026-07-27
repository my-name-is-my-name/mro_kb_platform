from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.go_no_go import (
    CertificateCatalog,
    CertificateEntry,
    GoNoGoService,
    InternalEvidenceRetriever,
    TSearchRetriever,
)
from storage.sqlite.store import SQLiteStore


class GoNoGoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.certificate = CertificateCatalog()

    def make_service(self) -> GoNoGoService:
        path = Path(tempfile.mktemp(suffix=".sqlite3"))
        store = SQLiteStore(path)
        store.initialize()
        with patch.dict("os.environ", {"MRO_KB_ATA_AGENT_LLM_ENABLED": "0"}):
            return GoNoGoService(store)

    def test_certificate_catalog_reads_docx_and_matches_known_ata(self) -> None:
        self.assertTrue(self.certificate.entries)
        result = self.certificate.match(["ATA 53", "ATA 99"])
        self.assertEqual(result["status"], "out_of_scope")
        self.assertEqual(result["unmatched"], ["ATA 99"])
        self.assertEqual(result["matched"][0]["ata"], "ATA 53")

    def test_certificate_catalog_subchapter_matching_matrix(self) -> None:
        catalog = object.__new__(CertificateCatalog)
        catalog.path = Path("test-certificate.docx")
        catalog.entries = [
            CertificateEntry("25", "20", "twenty", ""),
            CertificateEntry("25", "10", "ten", ""),
        ]
        catalog.by_system = {"25": catalog.entries}

        exact = catalog.match(["ATA 25-10"])
        self.assertEqual(exact["status"], "in_scope_candidate")
        self.assertEqual(exact["matched"][0]["certificate_ata"], "ATA 25-10")
        self.assertEqual(exact["matched"][0]["name"], "ten")

        missing = catalog.match(["ATA 25-30"])
        self.assertEqual(missing["status"], "ambiguous")
        self.assertEqual(missing["matched"], [])
        self.assertEqual(missing["ambiguous"], ["ATA 25-30"])

        chapter = catalog.match(["ATA 25"])
        self.assertEqual(chapter["status"], "in_scope_candidate")
        self.assertEqual(chapter["matched"][0]["certificate_ata"], "ATA 25")
        self.assertEqual(chapter["matched"][0]["name"], "")

        unavailable = object.__new__(CertificateCatalog)
        unavailable.path = Path("missing.docx")
        unavailable.entries = []
        unavailable.by_system = {}
        unavailable_result = unavailable.match(["ATA 25-10"])
        self.assertEqual(unavailable_result["status"], "catalog_unavailable")
        self.assertFalse(unavailable_result["catalog_loaded"])
        self.assertEqual(unavailable_result["ambiguous"], ["ATA 25-10"])

    def test_direct_ata_repair_without_damage_dimensions_requests_info(self) -> None:
        result = self.make_service().triage("Ремонт повреждения фюзеляжа ATA 53 на Airbus A320")
        self.assertEqual(result["recommendation"], "need_more_info")
        self.assertIn("Запросить дополнительную информацию", result["answer"])
        self.assertIn("размеры/координаты повреждения", result["missing_inputs"])
        self.assertIn("фотографии повреждения", result["missing_inputs"])
        self.assertEqual(result["confirmed_affected_ata"], [])
        self.assertEqual(
            [item["ata"] for item in result["ata_impact"]["validated_ata"]["user_declared_unverified"]],
            ["ATA 53"],
        )
        self.assertTrue(result["needs_human_approval"])

    def test_modification_does_not_mix_certificate_scope_with_capability(self) -> None:
        result = self.make_service().triage("Модификация электрической системы ATA 24 на Airbus A320")
        self.assertNotEqual(result["recommendation"], "go_to_assessment")
        self.assertNotIn("ATA 51", result["potentially_affected_ata"])
        self.assertEqual(result["capability_screening"], "not_assessed")

    def test_unknown_scope_is_hold_or_no_go_not_go(self) -> None:
        result = self.make_service().triage("Работа на неизвестной системе ATA 99")
        self.assertNotEqual(result["recommendation"], "go_to_assessment")

    def test_tsearch_disabled_is_explicit(self) -> None:
        result = TSearchRetriever(url="")
        self.assertFalse(result.enabled)
        self.assertEqual(result.retrieve("test", {}, limit=1)["status"], "disabled")

    def test_additional_data_output_is_searchable_evidence(self) -> None:
        service = self.make_service()
        evidence = service.retriever._search_local_documents("технического обоснования отчета по прочности", 5)
        self.assertTrue(evidence)
        self.assertTrue(any(item["source_type"] == "additional_internal_document" for item in evidence))

    def test_fields_do_not_use_legacy_semantic_mapping_without_llm(self) -> None:
        result = self.make_service().triage(
            "Разработать ремонт трещины в районе шпангоута FR 35 фюзеляжа Airbus A320",
            {
                "aircraft_type": "Airbus A320",
                "component": "фюзеляж, шпангоут FR 35",
                "damage_type": "трещина",
                "documents_available": ["фотографии повреждения"],
            },
        )
        self.assertEqual(result["direct_ata"], [])
        self.assertEqual(result["ata_impact"]["decision"], "engineering_review_required")
        self.assertNotIn("фотографии повреждения", result["missing_inputs"])
        self.assertIn("размеры/координаты повреждения", result["missing_inputs"])

    def staged_result(self, **overrides: object) -> dict[str, object]:
        result: dict[str, object] = {
            "affected_ata": ["ATA 25"],
            "potentially_affected_ata": [],
            "context_ata": [],
            "required_input_data": [],
            "decision": "completed",
            "warnings": [],
            "engineering_facts": {"uncertainties": []},
            "validated_ata": {
                "document_verification_required": [],
                "candidate_unverified": [],
                "user_declared_unverified": [],
            },
            "certificate_scope": {
                "status": "in_scope_candidate",
                "catalog_loaded": True,
                "matched": [{"ata": "ATA 25"}],
                "unmatched": [],
            },
            "ata_mapping": {},
        }
        result.update(overrides)
        return result

    def triage_staged(self, staged: dict[str, object]) -> dict[str, object]:
        class StubImpact:
            def analyze(self, *args: object, **kwargs: object) -> dict[str, object]:
                return staged

        service = self.make_service()
        service.ata_impact = StubImpact()  # type: ignore[assignment]
        return service.triage(
            "Engineering work package",
            {
                "aircraft_type": "A320",
                "components": "cargo equipment",
                "work_type": "assessment",
            },
        )

    def test_context_only_never_goes_to_assessment(self) -> None:
        result = self.triage_staged(
            self.staged_result(
                affected_ata=[],
                context_ata=["ATA 53"],
                decision="engineering_review_required",
            )
        )
        self.assertEqual(result["recommendation"], "hold_expert_review")
        self.assertIn("технически затронутый объект не определён", result["explanation"])

    def test_empty_affected_never_goes_to_assessment(self) -> None:
        result = self.triage_staged(
            self.staged_result(
                affected_ata=[],
                decision="engineering_review_required",
            )
        )
        self.assertNotEqual(result["recommendation"], "go_to_assessment")

    def test_staged_required_input_and_uncertainty_block_assessment(self) -> None:
        required = self.triage_staged(
            self.staged_result(
                required_input_data=["effectivity"],
                decision="additional_input_required",
            )
        )
        self.assertEqual(required["recommendation"], "need_more_info")
        uncertain = self.triage_staged(
            self.staged_result(
                engineering_facts={"uncertainties": ["attachment involvement"]},
                decision="additional_input_required",
            )
        )
        self.assertEqual(uncertain["recommendation"], "need_more_info")

    def test_document_required_and_potential_ata_block_assessment(self) -> None:
        document = self.triage_staged(
            self.staged_result(
                potentially_affected_ata=["ATA 53"],
                validated_ata={
                    "document_verification_required": [{"candidate_id": "candidate:1"}],
                    "candidate_unverified": [],
                },
                decision="document_verification_required",
            )
        )
        self.assertEqual(document["recommendation"], "hold_expert_review")
        potential = self.triage_staged(
            self.staged_result(
                potentially_affected_ata=["ATA 32"],
                decision="completed_with_hypotheses",
            )
        )
        self.assertEqual(potential["recommendation"], "hold_expert_review")

    def test_valid_closed_staged_case_can_go_to_assessment(self) -> None:
        result = self.triage_staged(self.staged_result())
        self.assertEqual(result["recommendation"], "go_to_assessment")

    def test_unverified_user_declared_ata_blocks_assessment(self) -> None:
        result = self.triage_staged(
            self.staged_result(
                validated_ata={
                    "document_verification_required": [],
                    "candidate_unverified": [],
                    "user_declared_unverified": [
                        {
                            "candidate_id": (
                                "candidate:user_declared_ata:request:ATA_34:1"
                            ),
                            "ata": "ATA 34",
                        }
                    ],
                },
                decision="engineering_review_required",
            )
        )
        self.assertEqual(result["recommendation"], "hold_expert_review")
        self.assertNotEqual(result["recommendation"], "go_to_assessment")
        self.assertTrue(result["unresolved_ata"])

    def test_certificate_unavailable_or_out_of_scope_blocks_assessment(self) -> None:
        unavailable = self.triage_staged(
            self.staged_result(
                certificate_scope={
                    "status": "catalog_unavailable",
                    "catalog_loaded": False,
                    "matched": [],
                    "unmatched": [],
                },
                decision="engineering_review_required",
            )
        )
        self.assertEqual(unavailable["recommendation"], "hold_expert_review")
        outside = self.triage_staged(
            self.staged_result(
                certificate_scope={
                    "status": "out_of_scope",
                    "catalog_loaded": True,
                    "matched": [],
                    "unmatched": ["ATA 99"],
                },
                affected_ata=["ATA 99"],
                decision="engineering_review_required",
            )
        )
        self.assertEqual(outside["recommendation"], "hold_expert_review")


if __name__ == "__main__":
    unittest.main()
