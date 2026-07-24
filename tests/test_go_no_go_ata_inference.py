import unittest

from core.go_no_go import GoNoGoService
from storage.sqlite.store import SQLiteStore


class AtaInferenceTests(unittest.TestCase):
    def test_static_pressure_receiver_maps_to_ata_34(self) -> None:
        service = GoNoGoService(SQLiteStore(":memory:"))
        result = service.triage(
            "Устранение царапин в районе приемника статического давления",
            {"aircraft_type": "Airbus A320", "components": "приемник статического давления"},
        )
        self.assertIn("ATA 34", result["direct_ata"])
        self.assertEqual(result["confirmed_affected_ata"], [])
        self.assertEqual(result["capability_screening"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
