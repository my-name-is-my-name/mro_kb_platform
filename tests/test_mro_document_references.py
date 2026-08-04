from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ingest.mro_docs.import_documents import import_mro_documents
from storage.sqlite.store import SQLiteStore


class MroDocumentReferenceTests(unittest.TestCase):
    def test_import_extracts_reference_table_and_chunk_citations(self) -> None:
        payload = {
            "work_order_id": "792",
            "aircraft_type": "A350",
            "msn": "00429",
            "subject": "Damage on right main landing gear axle",
            "problem_summary": "Inspection found mechanical damage.",
            "ata_list": ["32-10"],
            "applicable_ap_refs": ["25.1"],
            "documents": [
                {
                    "document_id": "792::psr::doc",
                    "title": "МР-792-ПСР",
                    "subject": "Пояснительная записка",
                    "document_family": "psr",
                    "source_file": "МР-792/МР-792-ПСР_A.md",
                    "chunks": [
                        {
                            "chunk_id": "source-chunk-1",
                            "chunk_kind": "paragraph",
                            "chunk_level": "section",
                            "section_title": "6 Описание системы",
                            "section_label": "6",
                            "heading_path": ["6 Описание системы"],
                            "text": "Ремонт выполняется по документам [2] и [7].",
                            "search_text": "",
                            "source_file": "МР-792/МР-792-ПСР_A.md",
                        }
                    ],
                    "tables": [
                        {
                            "table_id": "792::psr::doc::table_006",
                            "section_title": "4 Ссылочная документация",
                            "section_label": "4",
                            "heading_path": ["4 Ссылочная документация"],
                            "markdown": (
                                "|  | Авиационные правила, Часть 25 |\n"
                                "|---|---|\n"
                                "|  | Airbus SRM A350-A-32-10-11 |\n"
                                "|  | FATA-01047A Technical assessment |\n"
                                "|  | Drawing 1 |\n"
                                "|  | Drawing 2 |\n"
                                "|  | Drawing 3 |\n"
                                "|  | CMM 32-11-05 |\n"
                            ),
                            "metadata": {"table_kind": "generic"},
                            "source_file": "МР-792/МР-792-ПСР_A.md",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "792.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            cases, documents, chunks, references, _ = import_mro_documents(root)

            self.assertEqual(cases[0].case_id, "MRO-792")
            self.assertEqual([reference.ref_id for reference in references], ["1", "2", "3", "4", "5", "6", "7"])
            cited_chunk = next(chunk for chunk in chunks if chunk.chunk_kind == "paragraph")
            self.assertEqual(cited_chunk.citation_refs, ["2", "7"])

            store = SQLiteStore(root / "test.sqlite3")
            store.initialize()
            links = [(row["case_id"], row["document_id"], "matched") for row in documents]
            store.replace_cases(cases)
            store.replace_documents_and_chunks(documents, chunks, links, references)

            resolved = store.resolve_chunk_references(cited_chunk.chunk_id)
            self.assertEqual([item["ref_id"] for item in resolved], ["2", "7"])
            self.assertIn("Airbus SRM", str(resolved[0]["raw_text"]))
            self.assertIn("CMM", str(resolved[1]["raw_text"]))


if __name__ == "__main__":
    unittest.main()
