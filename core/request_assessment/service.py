from __future__ import annotations

import os
import platform
import uuid
from typing import Any, Callable

from storage.assessment_store import AssessmentStore

from .approval import assess_approval
from .ata_context import normalize_ata_context
from .capability import CapabilityProvider, JsonCapabilityProvider, build_capability_context
from .clients.ata_impact import AtaImpactClient
from .clients.base import ExternalServiceError, HttpClientConfig
from .clients.mro_kb import MroKbClient
from .clients.similar_cases import SimilarCasesClient
from .completeness import assess_completeness, has_blocking_customer_missing
from .decision import make_decision
from .evidence import assess_documentary_evidence, normalize_mro_kb_evidence
from .historical import build_historical_inference, extract_historical_facts, quotation_readiness
from .models import (
    AssessmentDecision,
    AssessmentState,
    AssessmentStatus,
    BusinessContext,
    ClarificationAnswer,
    ConfirmedInputs,
    DecisionReason,
    HumanReview,
    SourceInput,
    CapabilityRegistryMode,
)
from .progress import ProgressEvent
from .questions import build_questions
from .query_builders.mro_kb import build_case_query, build_wide_query
from .query_builders.similar_cases import build_similar_cases_payload
from .similar_cases import has_embedded_similar_cases, is_similar_cases_unavailable, qualified_cases, select_similar_cases

ProgressSink = Callable[[ProgressEvent], None]


class RequestAssessmentService:
    def __init__(
        self,
        ata_client: AtaImpactClient | None = None,
        mro_kb_client: MroKbClient | None = None,
        similar_cases_client: SimilarCasesClient | None = None,
        capability_provider: CapabilityProvider | None = None,
        store: AssessmentStore | None = None,
        allow_similar_cases_fallback: bool | None = None,
    ) -> None:
        self.ata_client = ata_client or AtaImpactClient(_client_config("MRO_ASSESSMENT_ATA_URL", "http://127.0.0.1:8122/api/ata-impact"))
        self.mro_kb_client = mro_kb_client or MroKbClient(_client_config("MRO_ASSESSMENT_MRO_KB_URL", "http://127.0.0.1:8121/api/chat"))
        self.similar_cases_client = similar_cases_client or SimilarCasesClient(_client_config("MRO_ASSESSMENT_SIMILAR_CASES_URL", "http://127.0.0.1:8121/api/similar-cases/search"))
        registry_path = os.environ.get("MRO_ASSESSMENT_CAPABILITY_REGISTRY", "config/request_assessment_capabilities.json")
        self.capability_provider = capability_provider or JsonCapabilityProvider(registry_path)
        self.store = store or AssessmentStore(os.environ.get("MRO_ASSESSMENT_DB_PATH", "data_runtime/request_assessment.sqlite3"))
        self.allow_similar_cases_fallback = _env_bool("MRO_ASSESSMENT_SIMILAR_CASES_FALLBACK_ENABLED", False) if allow_similar_cases_fallback is None else allow_similar_cases_fallback

    def create_assessment(self, payload: dict[str, Any], progress: ProgressSink | None = None) -> AssessmentState:
        state = AssessmentState(
            request_id=str(payload.get("request_id") or _new_request_id()),
            status=AssessmentStatus.RECEIVED,
            source=SourceInput(request_text=str(payload.get("request") or payload.get("q") or ""), attachments=list(payload.get("attachments") or [])),
            business_context=BusinessContext(**(payload.get("business_context") if isinstance(payload.get("business_context"), dict) else {})),
            confirmed_inputs=ConfirmedInputs(**_known_confirmed_inputs(payload.get("fields") if isinstance(payload.get("fields"), dict) else {})),
            confirmed_additional_data=_unknown_fields(payload.get("fields") if isinstance(payload.get("fields"), dict) else {}),
        )
        self._emit(progress, state, "received", "Получена новая MRO-заявка.", "completed")
        return self._run_workflow(state, progress)

    def answer_questions(self, request_id: str, answers_payload: dict[str, Any], progress: ProgressSink | None = None) -> AssessmentState:
        state = self.get_assessment(request_id)
        if not state:
            raise KeyError(request_id)
        incoming = [ClarificationAnswer(**item) for item in answers_payload.get("answers", []) if isinstance(item, dict)]
        state.answers.extend(incoming)
        unresolved_before = {item.field for item in state.missing_information}
        question_by_id = {item.question_id: item for item in state.questions}
        for answer in incoming:
            question = question_by_id.get(answer.question_id)
            if question:
                _apply_answer(state, question.field, answer.answer, answer.attachments)
        state.workflow_iteration += 1
        self._emit(progress, state, "answers", "Получены ответы на уточняющие вопросы. Повторяю технический анализ.", "completed")
        new_state = self._run_workflow(state, progress)
        unresolved_after = {item.field for item in new_state.missing_information}
        repeated = sorted(unresolved_before & unresolved_after)
        if repeated:
            new_state.warnings.append("ANSWER_DID_NOT_RESOLVE_GAP")
            self._emit(progress, new_state, "answers", "Часть ответов не сняла исходный gap.", "completed", details={"warning": "ANSWER_DID_NOT_RESOLVE_GAP", "fields": repeated})
            self.store.save(new_state)
        return new_state

    def human_review(self, request_id: str, payload: dict[str, Any]) -> AssessmentState:
        state = self.get_assessment(request_id)
        if not state:
            raise KeyError(request_id)
        action = str(payload.get("action") or "").upper()
        comment = str(payload.get("comment") or "")
        if action == "OVERRIDE" and not comment.strip():
            raise ValueError("comment is required for OVERRIDE")
        final = payload.get("final_decision")
        state.human_review = HumanReview(action=action, final_decision=AssessmentDecision(final) if final else None, comment=comment)
        if action in {"CONFIRM", "OVERRIDE"}:
            state.status = AssessmentStatus.CLOSED
        elif action == "REQUEST_MORE_INFORMATION":
            state.status = AssessmentStatus.WAITING_FOR_INFORMATION
        else:
            raise ValueError("unknown review action")
        return self.store.save(state)

    def get_assessment(self, request_id: str) -> AssessmentState | None:
        return self.store.get(request_id)

    def health(self) -> dict[str, Any]:
        registry = self.capability_provider.health() if hasattr(self.capability_provider, "health") else {"status": "unknown", "mode": _registry_mode(self.capability_provider).value}
        storage_status = "available" if self.store.health().get("status") == "available" else "unavailable"
        status = "ok" if registry.get("mode") == CapabilityRegistryMode.CONTROLLED.value and storage_status == "available" else "degraded"
        return {
            "status": status,
            "service": "mro-request-assessment",
            "python_version": platform.python_version(),
            "components": {
                "storage": {"status": storage_status},
                "capability_registry": registry,
                "ata_impact": {"configured": True, "safe_url": self.ata_client.safe_url},
                "mro_kb": {"configured": True, "safe_url": self.mro_kb_client.safe_url},
                "similar_cases_fallback": {"enabled": self.allow_similar_cases_fallback, "safe_url": self.similar_cases_client.safe_url},
            },
        }

    def _run_workflow(self, state: AssessmentState, progress: ProgressSink | None) -> AssessmentState:
        state.status = AssessmentStatus.ANALYZING
        state.decision = None
        state.capability_assessment = None
        state.approval_assessment = None
        state.documentary_assessment = None
        state.mro_kb_evidence = []
        state.selected_similar_cases = []
        state.historical_inference = None
        state.quotation_readiness = "NEEDS_EXPERT_REVIEW"
        self.store.save(state)

        if state.workflow_iteration > 3:
            state.status = AssessmentStatus.WAITING_FOR_EXPERT
            state.decision = make_decision([], None, None, None, _registry_mode(self.capability_provider), extra_review_reasons=[DecisionReason(code="CLARIFICATION_LIMIT_REACHED", source="clarification_manager", message="Maximum clarification iterations reached.")])
            self._emit(progress, state, "clarification_limit", "Достигнут предел трех циклов уточнения. Передаю на экспертную проверку.", "completed")
            return self.store.save(state)

        if not state.source.request_text.strip():
            state.status = AssessmentStatus.FAILED
            state.decision = make_decision([], None, None, None, _registry_mode(self.capability_provider), extra_review_reasons=[DecisionReason(code="REQUEST_TEXT_MISSING", source="api", message="Request text is required.")])
            return self.store.save(state)

        if not self._call_ata_impact(state, progress):
            state.status = AssessmentStatus.WAITING_FOR_EXPERT
            state.decision = make_decision([], None, None, None, _registry_mode(self.capability_provider), extra_review_reasons=[DecisionReason(code="ATA_IMPACT_UNAVAILABLE", source="mro-ata-impact", message="ATA Impact service is unavailable.")])
            return self.store.save(state)

        ata_ctx = normalize_ata_context(state.ata_impact, _ata_fields(state), state.business_context.requested_deliverables, state.source.request_text)
        self._emit(progress, state, "ata_context", "Сформирован структурированный технический контекст ATA Impact v2.", "completed", details=ata_ctx.model_dump(mode="json"))
        state.selected_similar_cases = select_similar_cases(state.similar_cases)
        self._emit(
            progress,
            state,
            "historical_case_selection",
            f"Выбрано исторических заявок для проверки: {len(state.selected_similar_cases)}.",
            "completed",
            service="mro-similar-cases",
            details={"selected_case_ids": [case.case_id for case in state.selected_similar_cases], "selection_reasons": {case.case_id: case.reasons for case in state.selected_similar_cases}},
        )
        state.missing_information = assess_completeness(state)
        state.questions = build_questions(state.missing_information)
        documentary = self._maybe_call_mro_kb(state, progress)
        state.documentary_assessment = documentary
        if has_blocking_customer_missing(state.missing_information):
            state.status = AssessmentStatus.WAITING_FOR_INFORMATION
            state.quotation_readiness = quotation_readiness(state)
            state.decision = make_decision(state.missing_information, None, None, state.documentary_assessment, _registry_mode(self.capability_provider))
            self._emit(progress, state, "completeness", "ATA Impact обнаружил блокирующие пробелы. Формальная capability/approval проверка отложена до уточнения данных.", "completed", details={"missing_count": len(state.missing_information)})
            return self.store.save(state)

        context = build_capability_context(state)

        self._emit(progress, state, "capability", "Проверяю базовое соответствие capability.", "started", details={"aircraft_family": context.aircraft_family, "affected_ata": context.affected_ata})
        capability = self.capability_provider.assess(context)
        state.capability_assessment = capability
        self._emit(progress, state, "capability", "Capability Registry проверен.", "completed", details={"mode": capability.registry_mode.value, "version": capability.registry_version, "status": capability.status.value, "dimensions": {key: value.value for key, value in capability.dimension_results.items()}})
        if capability.status.value == "FAIL" and capability.registry_mode == CapabilityRegistryMode.CONTROLLED:
            state.decision = make_decision([], capability, None, state.documentary_assessment, capability.registry_mode)
            state.status = AssessmentStatus.DECISION_READY
            self._emit(progress, state, "capability", "Capability pre-check выявил подтвержденный hard fail.", "completed")
            return self.store.save(state)

        approval = assess_approval(context, capability)
        state.approval_assessment = approval
        self._emit(progress, state, "approval", "Проверяю маршрут одобрения.", "completed", details={"status": approval.status.value})
        state.decision = make_decision([], capability, approval, documentary, capability.registry_mode)
        state.status = AssessmentStatus.WAITING_FOR_EXPERT if state.decision.status == AssessmentDecision.EXPERT_REVIEW else AssessmentStatus.DECISION_READY
        self._emit(progress, state, "decision", "Формирую предварительную рекомендацию.", "completed", details={"decision": state.decision.status.value})
        return self.store.save(state)

    def _call_ata_impact(self, state: AssessmentState, progress: ProgressSink | None) -> bool:
        fields = _ata_fields(state)
        self._emit(progress, state, "ata_impact", "Выполняю технический анализ через сервис mro-ata-impact.", "started", service="mro-ata-impact", safe_url=self.ata_client.safe_url, details={"purpose": "ATA Impact v2 and embedded similar cases"})
        try:
            payload, trace = self.ata_client.analyze(state.source.request_text, fields)
            state.external_call_trace.append(trace)  # type: ignore[arg-type]
        except ExternalServiceError as exc:
            state.external_call_trace.append(exc.trace)
            state.warnings.append("ATA_IMPACT_UNAVAILABLE")
            self._emit(progress, state, "ata_impact", "ATA Impact недоступен. Автоматическое техническое профилирование не выполняется.", "failed", service="mro-ata-impact", safe_url=self.ata_client.safe_url, details={"warning": exc.trace.warning})
            return False
        state.ata_impact = payload.get("ata_impact") if isinstance(payload.get("ata_impact"), dict) else payload
        embedded = payload.get("similar_cases") if isinstance(payload.get("similar_cases"), dict) else None
        state.similar_cases = embedded
        self._emit(progress, state, "ata_impact", "ATA Impact завершён.", "completed", service="mro-ata-impact", safe_url=self.ata_client.safe_url, details={"http_status": state.external_call_trace[-1].http_status, "elapsed_ms": state.external_call_trace[-1].elapsed_ms})
        if has_embedded_similar_cases(embedded):
            self._emit(progress, state, "similar_cases", "Сервис mro-ata-impact также выполнил поиск похожих заявок через mro-similar-cases.", "completed", service="mro-similar-cases", details=_similar_counts(embedded))
        elif is_similar_cases_unavailable(embedded):
            state.warnings.append("SIMILAR_CASES_EMBEDDED_UNAVAILABLE")
            if self.allow_similar_cases_fallback:
                self._call_similar_cases_fallback(state, progress)
            else:
                self._emit(progress, state, "similar_cases", "Встроенный поиск похожих заявок недоступен. Продолжаю оценку без исторических аналогов.", "skipped", service="mro-similar-cases")
        return True

    def _call_similar_cases_fallback(self, state: AssessmentState, progress: ProgressSink | None) -> None:
        fallback_payload = build_similar_cases_payload(state)
        self._emit(progress, state, "similar_cases_fallback", "Выполняю один fallback-вызов mro-similar-cases.", "started", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url, details={"reason": "embedded similar cases unavailable", "payload": fallback_payload})
        try:
            payload, trace = self.similar_cases_client.search(state.source.request_text, fallback_payload)
            state.external_call_trace.append(trace)  # type: ignore[arg-type]
            state.similar_cases = payload
            self._emit(progress, state, "similar_cases_fallback", "Fallback-поиск похожих заявок завершён.", "completed", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url, details=_similar_counts(payload))
        except ExternalServiceError as exc:
            state.external_call_trace.append(exc.trace)
            state.warnings.append("SIMILAR_CASES_FALLBACK_UNAVAILABLE")
            self._emit(progress, state, "similar_cases_fallback", "Fallback-поиск похожих заявок недоступен. Продолжаю без аналогов.", "failed", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url)

    def _maybe_call_mro_kb(self, state: AssessmentState, progress: ProgressSink | None):
        if not _mro_kb_needed(state):
            self._emit(progress, state, "mro_kb_search", "Документальная проверка MRO RAG пропущена: результат не влияет на текущую рекомендацию.", "skipped", service="mro-kb")
            warnings = ["historical_rag_not_required"]
            if state.selected_similar_cases:
                warnings.append("HISTORICAL_CANDIDATES_NOT_VERIFIED")
            state.historical_inference = build_historical_inference(state, [], warnings)
            state.quotation_readiness = quotation_readiness(state)
            return assess_documentary_evidence(False, [], [])
        unavailable = False
        extracted_facts = []
        extraction_warnings: list[str] = []
        ctx = normalize_ata_context(state.ata_impact, _ata_fields(state), state.business_context.requested_deliverables, state.source.request_text)
        if state.selected_similar_cases:
            for case in state.selected_similar_cases:
                query = build_case_query(case, state)
                self._emit(progress, state, "mro_kb_search", f"Проверяю {case.case_id} через MRO KB.", "started", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"case_id": case.case_id, "query": query})
                try:
                    payload, trace = self.mro_kb_client.chat(query)
                    state.external_call_trace.append(trace)  # type: ignore[arg-type]
                    records = normalize_mro_kb_evidence(case.case_id, payload, ctx)
                    state.mro_kb_evidence.extend(records)
                    facts, warnings = extract_historical_facts(case.case_id, payload, records)
                    extracted_facts.extend(facts)
                    extraction_warnings.extend(warnings)
                    self._emit(progress, state, "mro_kb_search", "MRO KB вернул исторические материалы.", "completed", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"case_id": case.case_id, "http_status": trace.http_status, "elapsed_ms": trace.elapsed_ms, "sources": len(payload.get("sources") or []), "evidence": len(payload.get("evidence") or []), "extracted_facts": len(facts), "warnings": warnings})
                except ExternalServiceError as exc:
                    state.external_call_trace.append(exc.trace)
                    state.warnings.append("DOCUMENTAL_VERIFICATION_UNAVAILABLE")
                    unavailable = True
                    extraction_warnings.append(str(exc.trace.warning or "mro_kb_unavailable"))
                    self._emit(progress, state, "mro_kb_search", "MRO KB недоступен. Это не трактуется как отсутствие документов.", "failed", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"case_id": case.case_id, "http_status": exc.trace.http_status, "warning": exc.trace.warning})
        elif not qualified_cases(state.similar_cases):
            query = build_wide_query(state)
            self._emit(progress, state, "mro_kb_wide_search", "Qualified similar cases не найдены. Выполняю широкий документальный поиск.", "started", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"query": query})
            try:
                payload, trace = self.mro_kb_client.chat(query)
                state.external_call_trace.append(trace)  # type: ignore[arg-type]
                records = normalize_mro_kb_evidence(None, payload, ctx)
                state.mro_kb_evidence.extend(records)
                facts, warnings = extract_historical_facts(None, payload, records)
                extracted_facts.extend(facts)
                extraction_warnings.extend(warnings)
                self._emit(progress, state, "mro_kb_wide_search", "MRO KB wide search завершён.", "completed", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"http_status": trace.http_status, "elapsed_ms": trace.elapsed_ms, "sources": len(payload.get("sources") or []), "evidence": len(payload.get("evidence") or []), "extracted_facts": len(facts), "warnings": warnings})
            except ExternalServiceError as exc:
                state.external_call_trace.append(exc.trace)
                state.warnings.append("DOCUMENTAL_VERIFICATION_UNAVAILABLE")
                unavailable = True
                extraction_warnings.append(str(exc.trace.warning or "mro_kb_unavailable"))
        documentary = assess_documentary_evidence(True, state.selected_similar_cases, state.mro_kb_evidence, state.warnings, unavailable)
        state.historical_inference = build_historical_inference(state, extracted_facts, extraction_warnings, unavailable)
        state.quotation_readiness = quotation_readiness(state, unavailable)
        self._emit(progress, state, "historical_inference", "Формирую preliminary proposed scope.", "completed", service="mro-kb", details={"selected_case_ids": state.historical_inference.selected_case_ids, "facts": len(state.historical_inference.facts), "proposed_scope": len(state.historical_inference.proposed_scope), "historical_support": state.historical_inference.historical_support, "quotation_readiness": state.quotation_readiness, "warnings": state.historical_inference.warnings})
        self._emit(progress, state, "documentary_assessment", "Документальная проверка оценена.", "completed", service="mro-kb", details={"status": documentary.status.value, "requested_case_ids": documentary.requested_case_ids, "usable_evidence": len(documentary.usable_evidence_ids), "warnings": documentary.warnings})
        return documentary

    def _emit(self, progress: ProgressSink | None, state: AssessmentState, stage: str, message: str, status: str, service: str | None = None, safe_url: str | None = None, details: dict[str, Any] | None = None) -> None:
        if progress:
            progress(ProgressEvent(stage=stage, message=message, status=status, service=service, safe_url=safe_url, request_id=state.request_id, details=details or {}))


def _client_config(env_name: str, default_url: str) -> HttpClientConfig:
    return HttpClientConfig(
        url=os.environ.get(env_name, default_url),
        timeout_seconds=float(os.environ.get("MRO_ASSESSMENT_HTTP_TIMEOUT_SECONDS", "10")),
        retries=1,
        enabled=True,
    )


def _new_request_id() -> str:
    return f"REQ-2026-{uuid.uuid4().hex[:8].upper()}"


def _known_confirmed_inputs(fields: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "aircraft.msn": "msn",
        "msn": "msn",
        "aircraft.registration": "registration",
        "registration": "registration",
        "aircraft.aircraft_type": "aircraft_type",
        "aircraft_type": "aircraft_type",
        "aircraft.model": "aircraft_model",
        "aircraft_model": "aircraft_model",
        "part_number": "part_number",
        "component.part_number": "part_number",
        "zone": "zone",
        "location.zone": "zone",
    }
    result: dict[str, Any] = {}
    for key, value in fields.items():
        target = mapping.get(key)
        if target and value is not None:
            result[target] = value
    return result


def _unknown_fields(fields: dict[str, Any]) -> dict[str, Any]:
    known = {
        "aircraft.msn", "msn", "aircraft.registration", "registration", "aircraft.aircraft_type",
        "aircraft_type", "aircraft.model", "aircraft_model", "part_number", "component.part_number",
        "zone", "location.zone",
    }
    return {key: value for key, value in fields.items() if key not in known}


def _apply_answer(state: AssessmentState, field: str, answer: Any, attachments: list[dict[str, Any]] | None = None) -> None:
    aliases = {
        "aircraft.msn": ("confirmed", "msn"),
        "msn": ("confirmed", "msn"),
        "aircraft.registration": ("confirmed", "registration"),
        "aircraft.aircraft_type": ("confirmed", "aircraft_type"),
        "aircraft.model": ("confirmed", "aircraft_model"),
        "aircraft_model": ("confirmed", "aircraft_model"),
        "part_number": ("confirmed", "part_number"),
        "component.part_number": ("confirmed", "part_number"),
        "zone": ("confirmed", "zone"),
        "location.zone": ("confirmed", "zone"),
        "requested_deliverables": ("business", "requested_deliverables"),
        "approval_expectation": ("business", "approval_expectation"),
    }
    target = aliases.get(field)
    if target and target[0] == "confirmed":
        setattr(state.confirmed_inputs, target[1], str(answer))
    elif target and target[0] == "business" and target[1] == "requested_deliverables":
        state.business_context.requested_deliverables = [str(item) for item in answer] if isinstance(answer, list) else [str(answer)]
    elif target and target[0] == "business":
        setattr(state.business_context, target[1], str(answer))
    else:
        state.confirmed_additional_data[field] = answer
    if field in {"damage.dimensions", "damage.location", "damage.type", "document_reference", "request.additional_data"}:
        state.confirmed_additional_data[field] = answer
    if attachments:
        state.confirmed_additional_data.setdefault("answer_attachments", []).extend(attachments)


def _ata_fields(state: AssessmentState) -> dict[str, Any]:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    for key, value in state.confirmed_additional_data.items():
        fields[key] = value
    aliases = {
        "damage.dimensions": "damage_dimensions",
        "damage.location": "damage_location",
        "damage.type": "damage_type",
    }
    for source, target in aliases.items():
        if source in state.confirmed_additional_data:
            fields[target] = state.confirmed_additional_data[source]
    if state.confirmed_additional_data:
        fields["confirmed_additional_data"] = dict(state.confirmed_additional_data)
    return fields


def _registry_mode(provider: object) -> CapabilityRegistryMode:
    value = getattr(provider, "mode", CapabilityRegistryMode.UNAVAILABLE)
    if isinstance(value, CapabilityRegistryMode):
        return value
    try:
        return CapabilityRegistryMode(str(value))
    except ValueError:
        return CapabilityRegistryMode.UNAVAILABLE


def _similar_counts(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "accepted": len(payload.get("accepted") or []),
        "not_accepted": len(payload.get("not_accepted") or []),
        "intermediate": len(payload.get("intermediate") or []),
        "threshold_version": payload.get("threshold_version") or "",
    }


def _mro_kb_needed(state: AssessmentState) -> bool:
    if state.selected_similar_cases:
        return any(case.similarity_class != "weak_analog" for case in state.selected_similar_cases)
    sim = state.similar_cases or {}
    return str(sim.get("similarity_status") or "") in {"no_qualified_matches", "none"} and bool((state.ata_impact or {}).get("affected_ata"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
