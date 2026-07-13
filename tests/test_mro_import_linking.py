from __future__ import annotations

import unittest

from ingest.mro_docs.import_documents import canonical_case_key


class ImportLinkingTests(unittest.TestCase):
    def test_canonical_case_key_extracts_digits(self) -> None:
        self.assertEqual(canonical_case_key("MP-0007"), "7")
        self.assertEqual(canonical_case_key("WO-007"), "7")
        self.assertEqual(canonical_case_key("MRO-941.01-ПСР_A"), "941")
