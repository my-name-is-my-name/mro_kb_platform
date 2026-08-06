from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import MethodType

from apps.request_assessment.server import RequestAssessmentHandler
from apps.request_assessment.server import _assessment_payload_from_chat
from core.request_assessment.capability import JsonCapabilityProvider
from core.request_assessment.clients.base import ExternalServiceError
from core.request_assessment.models import (
    ApprovalRouteAssessment,
    AssessmentResultStatus,
    CapabilityContext,
    CapabilityAssessment,
    CapabilityRegistryMode,
    DocumentaryAssessmentStatus,
    ExternalCallTrace,
)
from core.request_assessment.response_builder import build_final_content
from core.request_assessment.service import RequestAssessmentService
from core.request_assessment.progress import ProgressEvent, event_to_reasoning
from storage.assessment_store import AssessmentStore


class FakeAta:
    safe_url = "http://127.0.0.1:8122/api/ata-impact"

    def __init__(self, responses: list[dict[str, object]] | None = None, fail: bool = False) -> None:
        self.responses = responses or [_ata_response()]
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail = fail

    def analyze(self, request_text: str, fields: dict[str, object]) -> tuple[dict[str, object], ExternalCallTrace]:
        self.calls.append((request_text, fields))
        if self.fail:
            trace = ExternalCallTrace(service="mro-ata-impact", safe_url=self.safe_url, status="timeout", warning="timeout", attempts=2)
            raise ExternalServiceError("timeout", trace)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx], ExternalCallTrace(service="mro-ata-impact", safe_url=self.safe_url, status="success", http_status=200, elapsed_ms=10, attempts=1)


class FakeSimilar:
    safe_url = "http://127.0.0.1:8121/api/similar-cases/search"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, request_text: str, context: dict[str, object]) -> tuple[dict[str, object], ExternalCallTrace]:
        self.calls += 1
        self.last_payload = context
        return _similar("FB-1"), ExternalCallTrace(service="mro-similar-cases", safe_url=self.safe_url, status="success", http_status=200, attempts=1)


class FakeMroKb:
    safe_url = "http://127.0.0.1:8121/api/chat"

    def __init__(self, fail: bool = False, payload: dict[str, object] | None = None) -> None:
        self.queries: list[str] = []
        self.fail = fail
        self.payload = payload

    def chat(self, query: str) -> tuple[dict[str, object], ExternalCallTrace]:
        self.queries.append(query)
        if self.fail:
            trace = ExternalCallTrace(service="mro-kb", safe_url=self.safe_url, status="timeout", warning="timeout", attempts=2)
            raise ExternalServiceError("timeout", trace)
        payload = self.payload or {"ok": True, "answer": "found", "sources": [{"case_id": "MP-0123", "document_id": "DOC-1", "chunk_id": "C-1", "snippet": "fuselage skin crack ATA 53 repair", "applicability_status": "APPLICABLE"}], "evidence": []}
        return payload, ExternalCallTrace(service="mro-kb", safe_url=self.safe_url, status="success", http_status=200, attempts=1)


class StaticCapability:
    def __init__(self, status: AssessmentResultStatus, mode: CapabilityRegistryMode = CapabilityRegistryMode.CONTROLLED, routes: bool = True) -> None:
        self.status = status
        self.mode = mode
        self.version = "test"
        self.routes = routes

    def precheck(self, context):  # type: ignore[no-untyped-def]
        return self.assess(context)

    def assess(self, context):  # type: ignore[no-untyped-def]
        return CapabilityAssessment(
            status=self.status,
            registry_mode=self.mode,
            registry_version=self.version,
            matched_capability_ids=["CAP-1"],
            hard_fail_reasons=["outside scope"] if self.status == AssessmentResultStatus.FAIL else [],
            review_items=["review"] if self.status in {AssessmentResultStatus.UNKNOWN, AssessmentResultStatus.REVIEW} else [],
            matched_approval_routes=[ApprovalRouteAssessment(route="EXTERNAL_DOA", status=AssessmentResultStatus.PASS, jurisdictions=["EASA"], deliverables=["damage_assessment", "repair_drawing", "repair_instruction", "stress_substantiation"], source_document="Agreement", source_revision="Rev. 1")] if self.routes else [],
        )

    def health(self) -> dict[str, object]:
        return {"status": "available", "mode": self.mode.value, "version": self.version}


class RequestAssessmentWorkflowTests(unittest.TestCase):
    def make_service(self, ata: FakeAta | None = None, kb: FakeMroKb | None = None, similar: FakeSimilar | None = None, capability: object | None = None, fallback: bool = False) -> RequestAssessmentService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RequestAssessmentService(
            ata_client=ata or FakeAta(),  # type: ignore[arg-type]
            mro_kb_client=kb or FakeMroKb(),  # type: ignore[arg-type]
            similar_cases_client=similar or FakeSimilar(),  # type: ignore[arg-type]
            capability_provider=capability or StaticCapability(AssessmentResultStatus.PASS),  # type: ignore[arg-type]
            store=AssessmentStore(Path(tmp.name) / "assessment.sqlite3"),
            allow_similar_cases_fallback=fallback,
        )

    def test_embedded_similar_cases_reused_without_duplicate_call(self) -> None:
        similar = FakeSimilar()
        service = self.make_service(similar=similar)
        state = service.create_assessment(_request_payload())
        self.assertEqual(similar.calls, 0)
        self.assertEqual(state.similar_cases["accepted"][0]["case_id"], "MP-0123")  # type: ignore[index]

    def test_missing_similar_cases_fallback_at_most_once(self) -> None:
        similar = FakeSimilar()
        ata = FakeAta([_ata_response(similar_cases={"status": "disabled", "similarity_status": "unavailable"})])
        service = self.make_service(ata=ata, similar=similar, fallback=True)
        state = service.create_assessment(_request_payload())
        self.assertEqual(similar.calls, 1)
        self.assertEqual(state.similar_cases["accepted"][0]["case_id"], "FB-1")  # type: ignore[index]
        self.assertIn("limits", similar.last_payload)
        self.assertNotIn("msn", json.dumps(similar.last_payload).lower())

    def test_missing_msn_requests_information_after_mro_kb_candidate_check(self) -> None:
        kb = FakeMroKb()
        ata = FakeAta([_ata_response(required=[{"code": "AIRCRAFT_MSN_MISSING", "field": "msn", "importance": "required", "reason": "MSN needed"}])])
        service = self.make_service(ata=ata, kb=kb)
        payload = _request_payload()
        payload["fields"] = {"aircraft_type": "A320-214"}
        state = service.create_assessment(payload)
        self.assertEqual(state.decision.status.value, "REQUEST_INFORMATION")  # type: ignore[union-attr]
        self.assertEqual(len(kb.queries), 1)
        self.assertIn("По исторической заявке MP-0123", kb.queries[0])
        self.assertEqual(state.quotation_readiness, "NEEDS_INFORMATION")
        self.assertEqual(state.questions[0].field, "aircraft.msn")

    def test_blocking_missing_data_checks_embedded_candidates_with_mro_kb(self) -> None:
        kb = FakeMroKb()
        ata = FakeAta([
            _ata_response(
                required=[{"code": "AIRCRAFT_TYPE_MISSING", "field": "aircraft.type", "importance": "required", "reason": "Aircraft type needed"}],
                similar_cases=_similar_many("MP-", 5),
            )
        ])
        service = self.make_service(ata=ata, kb=kb)
        state = service.create_assessment({"request": "трещина в зоне гермошпангоута FR35"})
        content = build_final_content(state)

        self.assertEqual(state.decision.status.value, "REQUEST_INFORMATION")  # type: ignore[union-attr]
        self.assertEqual(state.quotation_readiness, "NEEDS_INFORMATION")
        self.assertEqual([case.case_id for case in state.selected_similar_cases], ["MP-1", "MP-2", "MP-3"])
        self.assertEqual(len(kb.queries), 3)
        self.assertNotEqual(state.historical_inference.historical_support, "CANDIDATES_ONLY")  # type: ignore[union-attr]
        self.assertNotIn("HISTORICAL_CANDIDATES_NOT_VERIFIED", state.historical_inference.warnings)  # type: ignore[union-attr]
        self.assertIn("MRO KB был проверен", content)
        self.assertNotIn("документальная проверка этих заявок через MRO KB пока не выполнялась", content)
        self.assertNotIn("пока не подтверждают применимость исторического work package", content)

    def test_blocking_missing_data_with_empty_similar_cases_can_report_no_candidates(self) -> None:
        kb = FakeMroKb()
        ata = FakeAta([
            _ata_response(
                required=[{"code": "AIRCRAFT_TYPE_MISSING", "field": "aircraft.type", "importance": "required", "reason": "Aircraft type needed"}],
                similar_cases={"status": "ok", "similarity_status": "no_qualified_matches", "accepted": [], "not_accepted": [], "intermediate": []},
            )
        ])
        service = self.make_service(ata=ata, kb=kb)
        state = service.create_assessment({"request": "трещина в зоне гермошпангоута FR35"})
        content = build_final_content(state)

        self.assertEqual(state.decision.status.value, "REQUEST_INFORMATION")  # type: ignore[union-attr]
        self.assertEqual(state.selected_similar_cases, [])
        self.assertEqual(len(kb.queries), 1)
        self.assertEqual(state.historical_inference.historical_support, "NONE")  # type: ignore[union-attr]
        self.assertIn("MRO KB был проверен, но документы не подтвердили", content)

    def test_blocking_missing_data_with_structured_mro_kb_facts_keeps_request_information(self) -> None:
        payload = {
            "ok": True,
            "facts": [
                {"category": "activity", "value": "damage assessment", "document_id": "DOC-1", "chunk_id": "C-1"},
                {"category": "document", "value": "repair drawing", "document_id": "DOC-2", "chunk_id": "C-2"},
            ],
            "sources": [{"document_id": "DOC-1", "chunk_id": "C-1", "snippet": "skin crack ATA 53 damage assessment", "applicability_status": "APPLICABLE"}],
            "evidence": [],
        }
        kb = FakeMroKb(payload=payload)
        ata = FakeAta([
            _ata_response(
                required=[{"code": "AIRCRAFT_TYPE_MISSING", "field": "aircraft.type", "importance": "required", "reason": "Aircraft type needed"}],
                similar_cases=_similar("MP-0123"),
            )
        ])
        service = self.make_service(ata=ata, kb=kb)
        state = service.create_assessment({"request": "трещина в зоне гермошпангоута FR35"})
        content = build_final_content(state)

        self.assertEqual(state.decision.status.value, "REQUEST_INFORMATION")  # type: ignore[union-attr]
        self.assertIsNone(state.capability_assessment)
        self.assertIsNone(state.approval_assessment)
        self.assertEqual(state.quotation_readiness, "NEEDS_INFORMATION")
        self.assertEqual(state.historical_inference.historical_support, "DIRECT")  # type: ignore[union-attr]
        self.assertIn("Историческая основа", content)
        self.assertIn("Предлагаемый предварительный scope", content)
        self.assertIn("damage assessment", content)

    def test_empty_mro_kb_result_under_blocking_missing_data_does_not_say_rag_not_performed(self) -> None:
        kb = FakeMroKb(payload={"ok": True, "answer": "", "sources": [], "evidence": []})
        ata = FakeAta([
            _ata_response(
                required=[{"code": "AIRCRAFT_TYPE_MISSING", "field": "aircraft.type", "importance": "required", "reason": "Aircraft type needed"}],
                similar_cases=_similar_many("MP-", 5),
            )
        ])
        service = self.make_service(ata=ata, kb=kb)
        state = service.create_assessment({"request": "трещина в зоне гермошпангоута FR35"})
        content = build_final_content(state)

        self.assertEqual(len(kb.queries), 3)
        self.assertEqual(state.historical_inference.historical_support, "NONE")  # type: ignore[union-attr]
        self.assertIn("MRO KB был проверен, но документы не подтвердили", content)
        self.assertNotIn("RAG was not performed", content)
        self.assertNotIn("MRO KB пока не выполнялась", content)
        self.assertNotIn("По историческим заявкам выполнялись", content)
        self.assertNotIn("Выпускались:", content)
        self.assertNotIn("Предлагаемый предварительный scope", content)
        self.assertNotIn("найдены подтверждающие evidence records", content)

    def test_answers_update_fields_rerun_ata_and_replace_similar_cases(self) -> None:
        ata = FakeAta([
            _ata_response(required=[{"field": "msn", "importance": "required", "reason": "MSN needed"}], similar_cases=_similar("OLD")),
            _ata_response(required=[], similar_cases=_similar("NEW")),
        ])
        service = self.make_service(ata=ata)
        first = service.create_assessment(_request_payload(fields={"aircraft_type": "A320-214"}))
        second = service.answer_questions(first.request_id, {"answers": [{"question_id": "Q-001", "answer": "4321"}]})
        self.assertEqual(second.confirmed_inputs.msn, "4321")
        self.assertEqual(len(ata.calls), 2)
        self.assertEqual(second.similar_cases["accepted"][0]["case_id"], "NEW")  # type: ignore[index]

    def test_similar_case_statuses_do_not_drive_decision(self) -> None:
        for payload in (_similar("ACCEPTED"), {"status": "ok", "similarity_status": "qualified_matches_found", "accepted": [], "not_accepted": [{"case_id": "NO", "similarity_reason_class": "same_identifier"}], "intermediate": []}, {"status": "ok", "similarity_status": "no_qualified_matches", "accepted": [], "not_accepted": [], "intermediate": []}):
            with self.subTest(payload=payload):
                service = self.make_service(ata=FakeAta([_ata_response(similar_cases=payload)]), kb=FakeMroKb(), capability=StaticCapability(AssessmentResultStatus.PASS))
                state = service.create_assessment(_request_payload())
                self.assertNotEqual(state.decision.status.value, "DECLINE")  # type: ignore[union-attr]

    def test_top_case_ids_get_separate_mro_kb_queries(self) -> None:
        kb = FakeMroKb()
        sim = {"status": "ok", "similarity_status": "qualified_matches_found", "accepted": [_case("A1"), _case("A2"), _case("A3"), _case("A4")], "not_accepted": [_case("R1"), _case("R2"), _case("R3")], "intermediate": [_case("I1"), _case("I2")]}
        service = self.make_service(ata=FakeAta([_ata_response(similar_cases=sim)]), kb=kb)
        state = service.create_assessment(_request_payload())
        self.assertEqual([case.case_id for case in state.selected_similar_cases], ["A1", "A2", "A3"])
        self.assertEqual(len(kb.queries), 3)
        self.assertIn("По исторической заявке A1", kb.queries[0])
        self.assertNotEqual(state.historical_inference.historical_support, "CANDIDATES_ONLY")  # type: ignore[union-attr]

    def test_mro_kb_timeout_yields_expert_review_not_no_documents(self) -> None:
        service = self.make_service(kb=FakeMroKb(fail=True))
        state = service.create_assessment(_request_payload())
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]
        self.assertIn("DOCUMENTAL_VERIFICATION_UNAVAILABLE", state.warnings)
        self.assertEqual(state.documentary_assessment.status, DocumentaryAssessmentStatus.UNAVAILABLE)
        self.assertEqual(state.historical_inference.historical_support, "UNAVAILABLE")  # type: ignore[union-attr]
        self.assertEqual(state.quotation_readiness, "NEEDS_EXPERT_REVIEW")

    def test_mro_kb_empty_result_is_inconclusive_not_decline(self) -> None:
        service = self.make_service(kb=FakeMroKb(payload={"ok": True, "answer": "", "sources": [], "evidence": []}))
        state = service.create_assessment(_request_payload())
        self.assertEqual(state.documentary_assessment.status, DocumentaryAssessmentStatus.INCONCLUSIVE)
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]

    def test_capability_outcomes(self) -> None:
        service = self.make_service(capability=StaticCapability(AssessmentResultStatus.FAIL))
        self.assertEqual(service.create_assessment(_request_payload()).decision.status.value, "DECLINE")  # type: ignore[union-attr]
        service = self.make_service(capability=StaticCapability(AssessmentResultStatus.UNKNOWN))
        self.assertEqual(service.create_assessment(_request_payload()).decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]

    def test_absence_of_analogs_runs_wide_search_and_does_not_decline(self) -> None:
        kb = FakeMroKb()
        service = self.make_service(ata=FakeAta([_ata_response(similar_cases={"status": "ok", "similarity_status": "none", "accepted": [], "not_accepted": [], "intermediate": []})]), kb=kb)
        state = service.create_assessment(_request_payload())
        self.assertNotEqual(state.decision.status.value, "DECLINE")  # type: ignore[union-attr]
        self.assertEqual(len(kb.queries), 1)
        self.assertIn("широкий документальный поиск", kb.queries[0])

    def test_advisory_registry_never_accepts(self) -> None:
        service = self.make_service(
            ata=FakeAta([_ata_response(similar_cases={"status": "ok", "similarity_status": "none", "accepted": [], "not_accepted": [], "intermediate": []})]),
            capability=StaticCapability(AssessmentResultStatus.PASS, mode=CapabilityRegistryMode.ADVISORY),
        )
        self.assertEqual(service.create_assessment(_request_payload()).decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]

    def test_default_registry_is_unavailable_and_does_not_accept_or_decline(self) -> None:
        provider = JsonCapabilityProvider("config/request_assessment_capabilities.json")
        service = self.make_service(
            ata=FakeAta([_ata_response(similar_cases={"status": "ok", "similarity_status": "none", "accepted": [], "not_accepted": [], "intermediate": []})]),
            capability=provider,
        )
        state = service.create_assessment(_request_payload())
        self.assertEqual(provider.mode, CapabilityRegistryMode.UNAVAILABLE)
        self.assertEqual(state.capability_assessment.status, AssessmentResultStatus.UNKNOWN)
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]

    def test_structured_mro_kb_result_extracts_historical_facts_and_scope(self) -> None:
        payload = {
            "ok": True,
            "answer": "structured",
            "facts": [
                {"category": "activity", "value": "damage assessment", "document_id": "DOC-1", "chunk_id": "C-1"},
                {"category": "calculation", "value": "stress substantiation", "document_id": "DOC-1", "chunk_id": "C-2"},
                {"category": "document", "value": "repair drawing", "document_id": "DOC-2", "chunk_id": "C-3"},
            ],
            "sources": [{"case_id": "MP-0123", "document_id": "DOC-1", "chunk_id": "C-1", "snippet": "fuselage skin crack ATA 53 repair", "applicability_status": "APPLICABLE"}],
            "evidence": [],
        }
        service = self.make_service(kb=FakeMroKb(payload=payload))
        state = service.create_assessment(_request_payload())
        inference = state.historical_inference
        self.assertEqual(inference.historical_support, "DIRECT")  # type: ignore[union-attr]
        self.assertEqual(state.quotation_readiness, "READY_FOR_ESTIMATION")
        self.assertTrue(all(fact.source_case_id == "MP-0123" for fact in inference.facts))  # type: ignore[union-attr]
        self.assertTrue(all(fact.evidence_ids for fact in inference.facts))  # type: ignore[union-attr]
        self.assertTrue(any(item.source_case_ids == ["MP-0123"] for item in inference.proposed_scope))  # type: ignore[union-attr]

    def test_invalid_json_and_free_text_do_not_break_workflow(self) -> None:
        payload = {"ok": True, "answer": "```json\n{\"facts\": [bad]\n```", "sources": [], "evidence": []}
        state = self.make_service(kb=FakeMroKb(payload=payload)).create_assessment(_request_payload())
        self.assertEqual(state.historical_inference.historical_support, "NONE")  # type: ignore[union-attr]
        self.assertIn("mro_kb_answer_json_block_invalid", state.historical_inference.warnings)  # type: ignore[union-attr]

    def test_sources_evidence_are_preserved_and_partial_materials_are_partial(self) -> None:
        payload = {
            "ok": True,
            "answer": "```json\n{\"activities\": [\"document review\"]}\n```",
            "sources": [{"case_id": "MP-0123", "document_id": "DOC-1", "chunk_id": "C-1", "snippet": "partial context", "applicability_status": "UNKNOWN"}],
            "evidence": [{"case_id": "MP-0123", "document_id": "DOC-2", "chunk_id": "C-2", "snippet": "partial evidence"}],
        }
        state = self.make_service(ata=FakeAta([_ata_response(similar_cases={"status": "ok", "similarity_status": "qualified_matches_found", "accepted": [_case("MP-0123", klass="same_work_type")], "not_accepted": [], "intermediate": []})]), kb=FakeMroKb(payload=payload)).create_assessment(_request_payload())
        self.assertEqual(len(state.mro_kb_evidence), 2)
        self.assertEqual(state.historical_inference.historical_support, "PARTIAL")  # type: ignore[union-attr]

    def test_historical_facts_do_not_create_false_capability_pass(self) -> None:
        payload = {"ok": True, "facts": [{"category": "activity", "value": "repair design", "document_id": "DOC-1", "chunk_id": "C-1"}], "sources": [], "evidence": []}
        state = self.make_service(kb=FakeMroKb(payload=payload), capability=StaticCapability(AssessmentResultStatus.UNKNOWN)).create_assessment(_request_payload())
        self.assertEqual(state.capability_assessment.status, AssessmentResultStatus.UNKNOWN)
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]

    def test_differences_are_derived_from_case_fields(self) -> None:
        historical = _case("MP-0123") | {"aircraft_model": "A321-200", "zone": "FR40-FR41", "component": "frame"}
        sim = {"status": "ok", "similarity_status": "qualified_matches_found", "accepted": [historical], "not_accepted": [], "intermediate": []}
        payload = {"ok": True, "facts": [{"category": "activity", "value": "damage assessment", "document_id": "DOC-1", "chunk_id": "C-1"}], "sources": [], "evidence": []}
        state = self.make_service(ata=FakeAta([_ata_response(similar_cases=sim)]), kb=FakeMroKb(payload=payload)).create_assessment(_request_payload())
        self.assertTrue(any("другая модель ВС" in item for item in state.historical_inference.differences))  # type: ignore[union-attr]
        self.assertTrue(any("другая зона" in item for item in state.historical_inference.differences))  # type: ignore[union-attr]

    def test_final_answer_contains_historical_sections_not_json_dump(self) -> None:
        payload = {"ok": True, "facts": [{"category": "activity", "value": "damage assessment", "document_id": "DOC-1", "chunk_id": "C-1"}], "sources": [{"case_id": "MP-0123", "document_id": "DOC-1", "chunk_id": "C-1", "snippet": "fuselage skin crack ATA 53 repair", "applicability_status": "APPLICABLE"}], "evidence": []}
        state = self.make_service(kb=FakeMroKb(payload=payload)).create_assessment(_request_payload())
        content = build_final_content(state)
        self.assertIn("Историческая основа", content)
        self.assertIn("Предлагаемый предварительный scope", content)
        self.assertIn("Отличия новой заявки", content)
        self.assertNotIn('"request_id"', content)

    def test_answers_store_additional_data_and_pass_to_ata(self) -> None:
        ata = FakeAta([
            _ata_response(required=[{"field": "damage.dimensions", "importance": "required", "reason": "Need dimensions"}]),
            _ata_response(required=[]),
        ])
        service = self.make_service(ata=ata)
        first = service.create_assessment(_request_payload())
        second = service.answer_questions(first.request_id, {"answers": [{"question_id": "Q-001", "answer": {"length": 32, "unit": "mm"}, "attachments": [{"name": "photo.jpg"}]}]})
        self.assertEqual(second.confirmed_additional_data["damage.dimensions"], {"length": 32, "unit": "mm"})
        self.assertIn("damage_dimensions", ata.calls[-1][1])
        self.assertEqual(second.confirmed_additional_data["answer_attachments"][0]["name"], "photo.jpg")

    def test_ata_v2_arrays_reach_mro_kb_query(self) -> None:
        kb = FakeMroKb()
        service = self.make_service(kb=kb)
        service.create_assessment(_request_payload())
        self.assertIn("fuselage skin", kb.queries[0])
        self.assertIn("crack", kb.queries[0])
        self.assertNotIn("объект: не указан", kb.queries[0])

    def test_controlled_registry_scope_dimensions(self) -> None:
        provider = JsonCapabilityProvider(_registry_file(self, [_capability()]))
        ctx = service_context()
        assessment = provider.assess(ctx)
        self.assertEqual(assessment.status, AssessmentResultStatus.PASS)
        self.assertEqual(assessment.dimension_results["ata_scope"], AssessmentResultStatus.PASS)

        provider = JsonCapabilityProvider(_registry_file(self, [_capability(ata_scope=["ATA 51"])]))
        self.assertEqual(provider.assess(ctx).status, AssessmentResultStatus.UNKNOWN)

        provider = JsonCapabilityProvider(_registry_file(self, [_capability(ata_scope=["ATA 53"])]))
        ctx.potentially_affected_ata = ["ATA 57"]
        self.assertEqual(provider.assess(ctx).dimension_results["ata_scope"], AssessmentResultStatus.REVIEW)

        ctx = service_context()
        provider = JsonCapabilityProvider(_registry_file(self, [_capability(disciplines=["structures", "design"])]))
        self.assertNotEqual(provider.assess(ctx).status, AssessmentResultStatus.PASS)

        provider = JsonCapabilityProvider(_registry_file(self, [_capability(deliverables=["damage_assessment"])]))
        self.assertNotEqual(provider.assess(ctx).status, AssessmentResultStatus.PASS)

        bad = _capability()
        bad.pop("source_document")
        provider = JsonCapabilityProvider(_registry_file(self, [bad]))
        self.assertNotEqual(provider.assess(ctx).status, AssessmentResultStatus.PASS)

    def test_clarification_limit(self) -> None:
        service = self.make_service()
        state = service.create_assessment(_request_payload())
        state.workflow_iteration = 4
        service.store.save(state)
        state = service.answer_questions(state.request_id, {"answers": []})
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]


class SocketlessHandlerTests(unittest.TestCase):
    def test_streaming_contains_reasoning_content_content_and_done(self) -> None:
        old = __import__("apps.request_assessment.server", fromlist=["SERVICE"]).SERVICE
        import apps.request_assessment.server as server

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server.SERVICE = RequestAssessmentService(
            ata_client=FakeAta(),  # type: ignore[arg-type]
            mro_kb_client=FakeMroKb(),  # type: ignore[arg-type]
            similar_cases_client=FakeSimilar(),  # type: ignore[arg-type]
            capability_provider=StaticCapability(AssessmentResultStatus.PASS),  # type: ignore[arg-type]
            store=AssessmentStore(Path(tmp.name) / "assessment.sqlite3"),
        )
        raw = json.dumps({"model": "mro-request-assessment", "stream": True, "messages": [{"role": "user", "content": "A320 repair ATA 53"}]}).encode()
        handler = object.__new__(RequestAssessmentHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.send_response = MethodType(lambda self, status: None, handler)
        handler.send_header = MethodType(lambda self, key, value: None, handler)
        handler.end_headers = MethodType(lambda self: None, handler)
        try:
            handler.do_POST()
            body = handler.wfile.getvalue().decode()
        finally:
            server.SERVICE = old
        self.assertIn("reasoning_content", body)
        self.assertIn("mro-ata-impact", body)
        self.assertIn("content", body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))

    def test_openai_payload_validation_rejects_empty_messages(self) -> None:
        for payload in ({}, {"messages": []}, {"messages": [{"role": "assistant", "content": "x"}]}, {"messages": [{"role": "user", "content": "  "}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    _assessment_payload_from_chat({"model": "mro-request-assessment", **payload})

    def test_reasoning_recursively_sanitizes_secrets(self) -> None:
        text = event_to_reasoning(
            ProgressEvent(
                stage="security",
                message="check",
                status="completed",
                request_id="REQ-1",
                details={"nested": {"Authorization": "Bearer secret", "safe": [{"token": "x", "value": "ok"}]}, "password": "bad"},
            )
        )
        self.assertNotIn("Bearer", text)
        self.assertNotIn("token", text)
        self.assertNotIn("password", text)
        self.assertIn("ok", text)


def _request_payload(fields: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "request": "A320-214 repair design for fuselage skin crack at ATA 53 with stress substantiation",
        "fields": fields or {"aircraft_type": "A320-214", "msn": "4321"},
        "business_context": {"requested_deliverables": ["damage_assessment", "repair_drawing", "repair_instruction", "stress_substantiation"], "jurisdiction": "EASA"},
    }


def _ata_response(required: list[object] | None = None, similar_cases: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "ata_impact": {
            "contract_version": "v2",
            "engineering_facts": {
                "physical_objects": [{"name": "fuselage skin"}],
                "structural_elements": [{"name": "skin panel"}],
                "damage": [{"type": "crack", "description": "crack from fastener hole"}],
                "locations": [{"name": "FR35-FR36"}],
                "event": {"type": "REPAIR_DESIGN", "maintenance_action": "develop structural repair"},
                "uncertainties": [],
            },
            "affected_ata": ["ATA 53"],
            "potentially_affected_ata": ["ATA 51"],
            "required_input_data": required or [],
            "decision": "technical_analysis_complete",
        },
        "similar_cases": similar_cases if similar_cases is not None else _similar("MP-0123"),
    }


def _similar(case_id: str) -> dict[str, object]:
    return {"status": "ok", "similarity_status": "qualified_matches_found", "threshold_version": "similarity-gate-v1", "accepted": [_case(case_id)], "not_accepted": [], "intermediate": []}


def _similar_many(prefix: str, count: int) -> dict[str, object]:
    return {
        "status": "ok",
        "similarity_status": "qualified_matches_found",
        "threshold_version": "similarity-gate-v1",
        "accepted": [_case(f"{prefix}{idx}") for idx in range(1, count + 1)],
        "not_accepted": [],
        "intermediate": [],
    }


def _case(case_id: str, klass: str = "same_identifier") -> dict[str, object]:
    return {"case_id": case_id, "similarity_reason_class": klass, "similarity_confidence": "high", "structured_score": 0.9, "semantic_score": 0.8, "reasons": ["same component"]}


def service_context() -> CapabilityContext:
    return CapabilityContext(
        aircraft_family="A320",
        aircraft_model="A320-214",
        work_type="REPAIR_DESIGN",
        affected_ata=["ATA 53"],
        potentially_affected_ata=["ATA 51"],
        disciplines=["structures", "stress", "design"],
        deliverables=["damage_assessment", "repair_drawing", "repair_instruction", "stress_substantiation"],
        jurisdiction="EASA",
    )


def _capability(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "capability_id": "CAP-1",
        "aircraft_family": "A320",
        "aircraft_models": ["A319", "A320", "A321"],
        "work_type": "REPAIR_DESIGN",
        "ata_scope": ["ATA 51", "ATA 53"],
        "disciplines": ["structures", "stress", "design"],
        "deliverables": ["damage_assessment", "repair_drawing", "repair_instruction", "stress_substantiation"],
        "approval_routes": ["EXTERNAL_DOA"],
        "jurisdictions": ["EASA"],
        "source_document": "Capability List",
        "source_revision": "Rev. 1",
        "verification_status": "APPROVED",
        "active": True,
    }
    payload.update(overrides)
    return payload


def _registry_file(test: unittest.TestCase, capabilities: list[dict[str, object]]) -> str:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "registry.json"
    path.write_text(json.dumps({"version": "test", "mode": "CONTROLLED", "verification_status": "APPROVED", "capabilities": capabilities}), encoding="utf-8")
    return str(path)


if __name__ == "__main__":
    unittest.main()
