from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.retrieval.service import RetrievalService
from core.retrieval.vector import MroQdrantIndex, MroVectorSettings, parent_id_for_chunk
from storage.sqlite.store import SQLiteStore


SAMPLE_CHUNK = {
    "chunk_id": "MRO-001::doc::abc::chunk::001",
    "case_id": "MRO-001",
    "document_id": "MRO-001::doc::abc",
    "source_document_id": "001::opi::doc",
    "document_family": "opi",
    "chunk_kind": "paragraph",
    "unit_kind": "chunk",
    "chunk_level": "section",
    "section_title": "5. Repair of lower wing panel",
    "section_label": "repair",
    "heading_path": ["Technical disposition", "Repair"],
    "subject": "Cracks around fastener holes",
    "problem_summary": "Cracks on lower panel of left wing",
    "aircraft_type": "ERJ170",
    "msn": "17000015",
    "ata_list": ["57-10"],
    "source_file": "МР-001/OPI.md",
    "vault_note_path": "МР-001/OPI.md",
    "block_id": "",
    "page_image_path": "",
    "text": "Install repair doubler in the damaged zone.",
    "search_text": "lower wing panel crack fastener repair doubler",
}


class MroVectorRetrievalTests(unittest.TestCase):
    def test_embedding_text_contains_mro_context(self) -> None:
        text = MroQdrantIndex.embedding_text(SAMPLE_CHUNK)

        self.assertIn("Заявка: MRO-001", text)
        self.assertIn("ATA: ['57-10']", text)
        self.assertIn("Cracks around fastener holes", text)
        self.assertIn("lower wing panel crack", text)

    def test_payload_keeps_source_fields(self) -> None:
        payload = MroQdrantIndex.payload_for_chunk(SAMPLE_CHUNK)

        self.assertEqual(payload["chunk_id"], SAMPLE_CHUNK["chunk_id"])
        self.assertEqual(payload["parent_id"], parent_id_for_chunk(SAMPLE_CHUNK))
        self.assertEqual(payload["parent_kind"], "section")
        self.assertEqual(payload["case_id"], "MRO-001")
        self.assertEqual(payload["source_document_id"], "001::opi::doc")
        self.assertEqual(payload["vault_note_path"], "МР-001/OPI.md")

    def test_hybrid_merge_deduplicates_and_prefers_vector_lexical_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            index = MroQdrantIndex(
                store,
                MroVectorSettings(
                    qdrant_url="http://unused",
                    collection="test",
                    embedding_url="http://unused",
                    embedding_model="bge-m3:test",
                    retrieval_top_k=10,
                    rrf_k=60,
                ),
            )
        vector_hit = {**SAMPLE_CHUNK, "vector_score": 0.9, "vector_rank": 1, "rrf_score": 1 / 61}
        lexical_hit = {**SAMPLE_CHUNK, "lexical_score": 8.0}
        other_hit = {**SAMPLE_CHUNK, "chunk_id": "other", "vector_score": 0.8, "vector_rank": 2, "rrf_score": 1 / 62}

        merged = index.hybrid_merge("lower wing panel crack", [vector_hit, other_hit], [lexical_hit], limit=5)

        self.assertEqual(merged[0]["chunk_id"], SAMPLE_CHUNK["chunk_id"])
        self.assertEqual(len([item for item in merged if item["chunk_id"] == SAMPLE_CHUNK["chunk_id"]]), 1)
        self.assertIn("hybrid_score", merged[0])

    def test_hybrid_merge_returns_one_child_per_parent_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            index = MroQdrantIndex(store, MroVectorSettings(qdrant_url="http://unused", collection="test"))
        parent_id = parent_id_for_chunk(SAMPLE_CHUNK)
        first = {**SAMPLE_CHUNK, "chunk_id": "child-1", "parent_id": parent_id, "vector_score": 0.9, "rrf_score": 1 / 61}
        second = {**SAMPLE_CHUNK, "chunk_id": "child-2", "parent_id": parent_id, "vector_score": 0.89, "rrf_score": 1 / 62}

        merged = index.hybrid_merge("lower wing panel crack", [first, second], [], limit=2)

        self.assertEqual([item["chunk_id"] for item in merged], ["child-1", "child-2"])

    def test_retrieval_service_falls_back_to_sqlite_candidates(self) -> None:
        class FailingVectorIndex:
            def search(self, *_: object, **__: object) -> tuple[list[dict[str, object]], list[str]]:
                return [], ["vector_failed"]

            def hybrid_merge(self, *_: object, **__: object) -> list[dict[str, object]]:
                raise AssertionError("hybrid_merge should not be called without vector hits")

            def health(self) -> dict[str, object]:
                return {}

        class FakeStore:
            def search_documents(self, *_: object, **__: object) -> list[dict[str, object]]:
                return []

            def search_cases(self, *_: object, **__: object) -> list[dict[str, object]]:
                return []

            def search_documents_for_cases(self, *_: object, **__: object) -> list[dict[str, object]]:
                return []

            def search_text(self, *_: object, **__: object) -> list[dict[str, object]]:
                return [{**SAMPLE_CHUNK, "lexical_score": 10.0}]

        service = RetrievalService(FakeStore())  # type: ignore[arg-type]
        service._vector_index = FailingVectorIndex()  # type: ignore[assignment]

        hits = service._collect_candidates("lower wing panel crack", limit=6)

        self.assertEqual(hits[0]["chunk_id"], SAMPLE_CHUNK["chunk_id"])
        self.assertEqual(service._last_vector_warnings, ["vector_failed"])


if __name__ == "__main__":
    unittest.main()
