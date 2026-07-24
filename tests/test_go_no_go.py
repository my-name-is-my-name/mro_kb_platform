from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.go_no_go import CertificateCatalog, GoNoGoService, InternalEvidenceRetriever, TSearchRetriever
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
        self.assertEqual(result["recommendation"], "hold_expert_review")
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


if __name__ == "__main__":
    unittest.main()
