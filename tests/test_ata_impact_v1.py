from __future__ import annotations

import unittest

from core.go_no_go import AtaImpactAgent


class AtaImpactV1Tests(unittest.TestCase):
    def test_intake_does_not_query_historical_or_document_retriever(self) -> None:
        class Retriever:
            def __init__(self) -> None:
                self.calls = 0

            def retrieve(self, *args: object, **kwargs: object) -> dict[str, object]:
                self.calls += 1
                return {"documents": []}

        retriever = Retriever()
        AtaImpactAgent(retriever=retriever).analyze("Повреждение фюзеляжа", {"aircraft_type": "A320"}, mode="rules_only")
        self.assertEqual(retriever.calls, 0)

    def test_static_port_is_system_scope_only(self) -> None:
        result = AtaImpactAgent().analyze(
            "Царапины у приемника статического давления", {"aircraft_type": "A320"}, mode="rules_only"
        )
        self.assertEqual([item["ata"] for item in result["direct_system_ata"]], ["ATA 34"])
        self.assertEqual(result["direct_structural_ata"], [])

    def test_skin_damage_is_structural_scope(self) -> None:
        result = AtaImpactAgent().analyze(
            "Вмятина и трещина обшивки фюзеляжа между FR 23 и FR 24", {"aircraft_type": "A320"}, mode="rules_only"
        )
        self.assertEqual([item["ata"] for item in result["direct_structural_ata"]], ["ATA 53"])

    def test_procedure_is_not_ata_scope(self) -> None:
        result = AtaImpactAgent().analyze("Выполнить NTM 51-10-08", {"aircraft_type": "A320"}, mode="rules_only")
        self.assertEqual(result["direct_ata"], [])
        self.assertEqual(result["procedure_references"][0]["type"], "NTM")

    def test_procedure_with_ata_marker_is_not_scope(self) -> None:
        result = AtaImpactAgent().analyze("Использовать AMM ATA 34-11", {"aircraft_type": "A320"}, mode="rules_only")
        self.assertEqual(result["direct_ata"], [])
        self.assertEqual(result["procedure_references"][0]["type"], "AMM")

    def test_unknown_object_requests_information(self) -> None:
        result = AtaImpactAgent().analyze("Выполнить работу на самолёте", mode="rules_only")
        self.assertEqual(result["decision"], "request_information")
        self.assertTrue(result["required_input_data"])


if __name__ == "__main__":
    unittest.main()
