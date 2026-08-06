from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from core.models.entities import (
    CaseFactsRequest,
    CaseFactsResponse,
    CaseSummary,
    DocumentChunk,
    DocumentReference,
)
from core.retrieval.case_facts import CaseFactsService, classify_reference_role
from storage.sqlite.store import SQLiteStore


class FakeVectorIndex:
    def __init__(self, hits: list[dict[str, object]] | None = None, warnings: list[str] | None = None) -> None:
        self.hits = hits or []
        self.warnings = warnings or []

    def search(self, *_: object, **__: object) -> tuple[list[dict[str, object]], list[str]]:
        return self.hits, self.warnings


class FakeLLM:
    def __init__(self, facts: list[dict[str, object]] | None = None, raw: str | None = None) -> None:
        self.facts = facts or []
        self.raw = raw

    def chat(self, *_: object, **__: object) -> str:
        return self.raw if self.raw is not None else json.dumps({"facts": self.facts})


class MroCaseFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp_dir.name) / "test.sqlite3")
        self.store.initialize()
        cases = [
            CaseSummary(case_id="MRO-100"),
            CaseSummary(case_id="MRO-200"),
            CaseSummary(case_id="MRO-300"),
        ]
        self.store.replace_cases(cases)
        documents = [
            {"document_id": "DOC-A", "case_id": "MRO-100", "title": "Engineering report"},
            {"document_id": "DOC-B", "case_id": "MRO-100", "title": "Second report"},
            {"document_id": "DOC-EMPTY", "case_id": "MRO-300", "title": "No chunks"},
        ]
        chunks = [
            self.chunk(
                "DOC-A",
                "CH-PROBLEM",
                "Inspection found a crack in the frame at station FR35 during scheduled maintenance.",
                [],
            ),
            self.chunk(
                "DOC-A",
                "CH-ACTIVITY",
                "Damage assessment was completed in accordance with Airbus SRM [2]. Repair development was completed for the damaged frame [2].",
                ["2"],
            ),
            self.chunk(
                "DOC-A",
                "CH-CALC",
                "Static strength calculation was completed using the approved stress methodology [3].",
                ["3"],
            ),
            self.chunk(
                "DOC-A",
                "CH-DOCUMENT",
                "Repair drawing RD-100 was issued as the final repair deliverable [4].",
                ["4"],
            ),
            self.chunk("DOC-A", "CH-HEADER", "Repair drawing", [], section_title="Repair drawing"),
            self.chunk(
                "DOC-B",
                "CH-B",
                "Inspection was completed in accordance with component manual [2].",
                ["2"],
            ),
        ]
        references = [
            self.reference("DOC-A", "2", "Airbus SRM A320 53-00", "SRM"),
            self.reference("DOC-A", "3", "Stress methodology SM-1", "methodology"),
            self.reference("DOC-A", "4", "Repair drawing RD-100", "drawing"),
            self.reference("DOC-A", "5", "AP-21 Engineering approval procedure", "procedure"),
            self.reference("DOC-A", "6", "Cover letter from operator", "email"),
            self.reference("DOC-A", "7", "Unclassified source Z", ""),
            self.reference("DOC-B", "2", "CMM 32-11-05", "CMM"),
        ]
        links = [(row["case_id"], row["document_id"], "matched") for row in documents]
        self.store.replace_documents_and_chunks(documents, chunks, links, references)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def chunk(
        document_id: str,
        chunk_id: str,
        text: str,
        refs: list[str],
        *,
        section_title: str = "Evidence",
    ) -> DocumentChunk:
        return DocumentChunk(
            case_id="MRO-100",
            document_id=document_id,
            chunk_id=chunk_id,
            chunk_kind="paragraph",
            section_title=section_title,
            text=text,
            search_text=text,
            citation_refs=refs,
        )

    @staticmethod
    def reference(document_id: str, ref_id: str, raw_text: str, document_type: str) -> DocumentReference:
        return DocumentReference(
            case_id="MRO-100",
            document_id=document_id,
            ref_id=ref_id,
            raw_text=raw_text,
            title=raw_text,
            document_type=document_type,
            source_document_id=f"source::{document_id}",
            source_table_id=f"table::{document_id}",
        )

    def service(self, facts: list[dict[str, object]] | None = None, **kwargs: object) -> CaseFactsService:
        return CaseFactsService(
            self.store,
            vector_index=kwargs.get("vector_index") or FakeVectorIndex(),  # type: ignore[arg-type]
            llm=kwargs.get("llm") if "llm" in kwargs else FakeLLM(facts),  # type: ignore[arg-type]
        )

    def fact(self, category: str, value: str, document_id: str, chunk_id: str, evidence: str) -> dict[str, str]:
        return {
            "category": category,
            "value": value,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "evidence_text": evidence,
        }

    def request(self, case_id: str = "MRO-100", categories: list[str] | None = None) -> CaseFactsRequest:
        return CaseFactsRequest(case_id=case_id, categories=categories or ["problem", "activity", "calculation", "document"])

    def test_exact_internal_id(self) -> None:
        resolution = self.store.resolve_case_id("MRO-100")
        self.assertEqual(resolution.resolution_method, "EXACT_INTERNAL_ID")
        self.assertEqual(resolution.resolved_case_id, "MRO-100")

    def test_non_internal_ids_are_unresolved_even_when_digits_match(self) -> None:
        for requested in ("MP-100", "WO-100", "MP-0100", "100"):
            with self.subTest(requested=requested):
                resolution = self.store.resolve_case_id(requested)
                self.assertEqual(resolution.resolution_method, "UNRESOLVED")
                self.assertIsNone(resolution.resolved_case_id)
                self.assertEqual(resolution.candidate_case_ids, [])

    def test_unknown_case(self) -> None:
        response = self.service([]).case_facts(self.request("UNKNOWN-999"))
        self.assertEqual(response.status, "CASE_NOT_FOUND")

    def test_case_without_documents(self) -> None:
        response = self.service([]).case_facts(self.request("MRO-200"))
        self.assertEqual(response.status, "CASE_FOUND_NO_DOCUMENTS")

    def test_case_with_documents_without_chunks(self) -> None:
        response = self.service([]).case_facts(self.request("MRO-300"))
        self.assertEqual(response.status, "CASE_FOUND_NO_CHUNKS")

    def test_grounded_facts_keep_existing_ids_and_exact_evidence(self) -> None:
        evidence = "Static strength calculation was completed using the approved stress methodology [3]."
        response = self.service([self.fact("calculation", "Static strength calculation", "DOC-A", "CH-CALC", evidence)]).case_facts(
            self.request(categories=["calculation"])
        )
        self.assertEqual(response.status, "FOUND")
        self.assertEqual(len(response.facts), 1)
        fact = response.facts[0]
        self.assertEqual(fact.document_id, "DOC-A")
        self.assertEqual(fact.chunk_id, "CH-CALC")
        self.assertIn(fact.evidence_text, str(self.store.fetch_chunk("CH-CALC")["text"]))  # type: ignore[index]

    def test_missing_evidence_unknown_id_and_cross_document_fact_are_dropped(self) -> None:
        facts = [
            self.fact("activity", "Damage assessment", "DOC-A", "CH-ACTIVITY", "not in chunk"),
            self.fact("activity", "Damage assessment", "DOC-A", "UNKNOWN", "Damage assessment was completed."),
            self.fact("activity", "Damage assessment", "DOC-B", "CH-ACTIVITY", "Damage assessment was completed"),
        ]
        response = self.service(facts).case_facts(self.request(categories=["activity"]))
        self.assertEqual(response.facts, [])
        self.assertIn("UNGROUNDED_FACT_DROPPED", response.warnings)
        self.assertIn("NO_GROUNDED_FACTS", response.warnings)

    def test_title_only_evidence_is_dropped(self) -> None:
        response = self.service(
            [self.fact("document", "Repair drawing", "DOC-A", "CH-HEADER", "Repair drawing")]
        ).case_facts(self.request(categories=["document"]))
        self.assertEqual(response.facts, [])
        self.assertIn("UNGROUNDED_FACT_DROPPED", response.warnings)

    def test_invalid_json_does_not_break_response(self) -> None:
        response = self.service(llm=FakeLLM(raw="not json")).case_facts(self.request())
        self.assertEqual(response.status, "FOUND")
        self.assertIn("FACT_EXTRACTION_UNAVAILABLE", response.warnings)

    def test_llm_unavailable_is_not_corpus_unavailable(self) -> None:
        response = self.service(llm=None).case_facts(self.request())
        self.assertEqual(response.status, "FOUND")
        self.assertEqual(response.facts, [])
        self.assertIn("FACT_EXTRACTION_UNAVAILABLE", response.warnings)

    def test_empty_facts_report_no_grounded_facts(self) -> None:
        response = self.service([]).case_facts(self.request())
        self.assertEqual(response.status, "FOUND")
        self.assertIn("NO_GROUNDED_FACTS", response.warnings)

    def test_cross_case_vector_hit_is_dropped(self) -> None:
        cross = {
            "case_id": "MRO-999",
            "document_id": "OTHER-DOC",
            "chunk_id": "OTHER-CHUNK",
            "text": "Damage assessment was completed for another case.",
        }
        response = self.service([], vector_index=FakeVectorIndex([cross])).case_facts(self.request())
        self.assertEqual(response.status, "FOUND")
        self.assertIn("CROSS_CASE_HIT_DROPPED", response.warnings)

    def test_qdrant_failure_uses_sqlite_fallback(self) -> None:
        response = self.service([], vector_index=FakeVectorIndex(warnings=["connection failed"])).case_facts(self.request())
        self.assertEqual(response.status, "FOUND")
        self.assertIn("QDRANT_UNAVAILABLE_SQLITE_FALLBACK", response.warnings)

    def test_total_retrieval_failure_has_retrieval_unavailable_status(self) -> None:
        service = self.service([], vector_index=FakeVectorIndex(warnings=["connection failed"]))
        with (
            patch.object(self.store, "search_text", side_effect=OSError("sqlite unavailable")),
            patch.object(self.store, "fetch_case_chunks", side_effect=OSError("sqlite unavailable")),
        ):
            response = service.case_facts(self.request())
        self.assertEqual(response.status, "RETRIEVAL_UNAVAILABLE")

    def test_reference_markers_are_resolved_within_evidence_document(self) -> None:
        evidence = "Damage assessment was completed in accordance with Airbus SRM [2]."
        response = self.service(
            [self.fact("activity", "Damage assessment", "DOC-A", "CH-ACTIVITY", evidence)]
        ).case_facts(self.request(categories=["activity"]))
        direct = response.facts[0].references
        self.assertEqual(len(direct), 1)
        self.assertIn("Airbus SRM", direct[0].raw_text)
        self.assertNotIn("CMM", direct[0].raw_text)
        self.assertEqual(direct[0].usage, "DIRECTLY_CITED")
        self.assertEqual(direct[0].role, "TECHNICAL_BASIS")

    def test_uncited_references_are_listed_only_and_raw_text_is_preserved(self) -> None:
        evidence = "Damage assessment was completed in accordance with Airbus SRM [2]."
        response = self.service(
            [self.fact("activity", "Damage assessment", "DOC-A", "CH-ACTIVITY", evidence)]
        ).case_facts(self.request(categories=["activity"]))
        self.assertTrue(response.listed_references)
        self.assertTrue(all(item.usage == "LISTED_ONLY" for item in response.listed_references))
        self.assertTrue(any(item.raw_text == "AP-21 Engineering approval procedure" for item in response.listed_references))

    def test_one_reference_can_support_multiple_facts(self) -> None:
        first = "Damage assessment was completed in accordance with Airbus SRM [2]."
        second = "Repair development was completed for the damaged frame [2]."
        response = self.service(
            [
                self.fact("activity", "Damage assessment", "DOC-A", "CH-ACTIVITY", first),
                self.fact("activity", "Repair development", "DOC-A", "CH-ACTIVITY", second),
            ]
        ).case_facts(self.request(categories=["activity"]))
        self.assertEqual(len(response.facts), 2)
        self.assertEqual([fact.references[0].ref_id for fact in response.facts], ["2", "2"])

    def test_reference_role_rules(self) -> None:
        examples = {
            "SRM 53-00": "TECHNICAL_BASIS",
            "Stress methodology SM-1": "ANALYSIS_BASIS",
            "Repair drawing RD-1": "HISTORICAL_DELIVERABLE",
            "AP-21 internal procedure": "PROCESS_REFERENCE",
            "Cover letter email": "CONTEXT_ONLY",
            "Unclassified source Z": "UNKNOWN",
        }
        for raw_text, expected in examples.items():
            with self.subTest(raw_text=raw_text):
                self.assertEqual(classify_reference_role({"raw_text": raw_text}), expected)

    def test_reference_deduplication_preserves_provenance(self) -> None:
        first = {"document_id": "DOC-A", "ref_id": "2", "source_document_id": "A", "source_table_id": "T1"}
        second = {"document_id": "DOC-B", "ref_id": "2", "source_document_id": "B", "source_table_id": "T2"}
        result = CaseFactsService._deduplicate_references([first, first.copy(), second])
        self.assertEqual(result, [first, second])

    def test_positive_and_negative_responses_share_schema(self) -> None:
        positive = self.service([]).case_facts(self.request()).model_dump()
        negative = self.service([]).case_facts(self.request("UNKNOWN-999")).model_dump()
        self.assertEqual(CaseFactsResponse.model_validate(positive).status, "FOUND")
        self.assertEqual(CaseFactsResponse.model_validate(negative).status, "CASE_NOT_FOUND")
        self.assertEqual(set(positive), set(negative))


class MroCaseFactsApiModelTests(unittest.TestCase):
    def test_request_and_response_models_validate_endpoint_contract(self) -> None:
        request = CaseFactsRequest.model_validate({"case_id": "MRO-100", "categories": ["activity"]})
        response = CaseFactsResponse(
            status="CASE_NOT_FOUND",
            requested_case_id=request.case_id,
            resolution_method="UNRESOLVED",
            resolution_evidence="no exact internal ID or verified alias",
        )
        self.assertEqual(CaseFactsResponse.model_validate(response.model_dump()).status, "CASE_NOT_FOUND")

    def test_unsupported_category_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as context:
            CaseFactsRequest.model_validate({"case_id": "MRO-100", "categories": ["capability"]})
        self.assertIn("categories.0", str(context.exception))

    def test_request_rejects_new_request_and_capability_context(self) -> None:
        forbidden_payloads = [
            {"case_id": "MRO-100", "request": "new damage"},
            {"case_id": "MRO-100", "capability_context": {"approved": True}},
            {"case_id": "MRO-100", "hours": 100},
        ]
        for payload in forbidden_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                CaseFactsRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
