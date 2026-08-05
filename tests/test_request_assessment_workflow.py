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
            matched_approval_routes=[ApprovalRouteAssessment(route="EXTERNAL_DOA", status=AssessmentResultStatus.PASS, jurisdictions=["EASA"], deliverables=["damage_assessment", "repair_drawing", "repair_instruction", "stress_substantiation"], source_document="Agreement", source_revision="Rev. 1")] if routes else [],
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

    def test_missing_msn_requests_information_and_skips_mro_kb(self) -> None:
        kb = FakeMroKb()
        ata = FakeAta([_ata_response(required=[{"code": "AIRCRAFT_MSN_MISSING", "field": "msn", "importance": "required", "reason": "MSN needed"}])])
        service = self.make_service(ata=ata, kb=kb)
        payload = _request_payload()
        payload["fields"] = {"aircraft_type": "A320-214"}
        state = service.create_assessment(payload)
        self.assertEqual(state.decision.status.value, "REQUEST_INFORMATION")  # type: ignore[union-attr]
        self.assertEqual(len(kb.queries), 0)
        self.assertEqual(state.questions[0].field, "aircraft.msn")

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
        self.assertEqual([case.case_id for case in state.selected_similar_cases], ["A1", "A2", "A3", "R1", "R2", "I1"])
        self.assertEqual(len(kb.queries), 6)
        self.assertIn("По заявке A1", kb.queries[0])

    def test_mro_kb_timeout_yields_expert_review_not_no_documents(self) -> None:
        service = self.make_service(kb=FakeMroKb(fail=True))
        state = service.create_assessment(_request_payload())
        self.assertEqual(state.decision.status.value, "EXPERT_REVIEW")  # type: ignore[union-attr]
        self.assertIn("DOCUMENTAL_VERIFICATION_UNAVAILABLE", state.warnings)
        self.assertEqual(state.documentary_assessment.status, DocumentaryAssessmentStatus.UNAVAILABLE)

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

    def test_pass_registry_can_accept_for_quotation_without_unneeded_rag(self) -> None:
        kb = FakeMroKb()
        service = self.make_service(ata=FakeAta([_ata_response(similar_cases={"status": "ok", "similarity_status": "none", "accepted": [], "not_accepted": [], "intermediate": []})]), kb=kb)
        state = service.create_assessment(_request_payload())
        self.assertEqual(state.decision.status.value, "ACCEPT_FOR_QUOTATION")  # type: ignore[union-attr]
        self.assertEqual(kb.queries, [])

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


def _case(case_id: str) -> dict[str, object]:
    return {"case_id": case_id, "similarity_reason_class": "same_identifier", "similarity_confidence": "high", "structured_score": 0.9, "semantic_score": 0.8, "reasons": ["same component"]}


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
