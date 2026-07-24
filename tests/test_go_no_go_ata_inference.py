import unittest
from unittest.mock import patch

from core.go_no_go import GoNoGoService
from storage.sqlite.store import SQLiteStore


class AtaInferenceTests(unittest.TestCase):
    def test_static_pressure_receiver_requires_llm_or_engineer(self) -> None:
        with patch.dict("os.environ", {"MRO_KB_ATA_AGENT_LLM_ENABLED": "0"}):
            service = GoNoGoService(SQLiteStore(":memory:"))
        result = service.triage(
            "Устранение царапин в районе приемника статического давления",
            {"aircraft_type": "Airbus A320", "components": "приемник статического давления"},
        )
        self.assertEqual(result["direct_ata"], [])
        self.assertEqual(result["confirmed_affected_ata"], [])
        self.assertEqual(result["ata_impact"]["decision"], "engineering_review_required")
        self.assertEqual(result["capability_screening"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
