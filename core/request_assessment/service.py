from __future__ import annotations

import os
import uuid
from typing import Any, Callable

from storage.assessment_store import AssessmentStore

from .approval import assess_approval
from .capability import CapabilityProvider, JsonCapabilityProvider, build_capability_context
from .clients.ata_impact import AtaImpactClient
from .clients.base import ExternalServiceError, HttpClientConfig
from .clients.mro_kb import MroKbClient
from .clients.similar_cases import SimilarCasesClient
from .completeness import assess_completeness, has_blocking_customer_missing
from .decision import make_decision
from .evidence import normalize_mro_kb_evidence
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
)
from .progress import ProgressEvent
from .questions import build_questions
from .query_builders.mro_kb import build_case_query, build_wide_query
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
            confirmed_inputs=ConfirmedInputs(**(payload.get("fields") if isinstance(payload.get("fields"), dict) else {})),
        )
        self._emit(progress, state, "received", "Получена новая MRO-заявка.", "completed")
        return self._run_workflow(state, progress)

    def answer_questions(self, request_id: str, answers_payload: dict[str, Any], progress: ProgressSink | None = None) -> AssessmentState:
        state = self.get_assessment(request_id)
        if not state:
            raise KeyError(request_id)
        incoming = [ClarificationAnswer(**item) for item in answers_payload.get("answers", []) if isinstance(item, dict)]
        state.answers.extend(incoming)
        question_by_id = {item.question_id: item for item in state.questions}
        for answer in incoming:
            question = question_by_id.get(answer.question_id)
            if question:
                _apply_answer(state, question.field, answer.answer)
        state.workflow_iteration += 1
        self._emit(progress, state, "answers", "Получены ответы на уточняющие вопросы. Повторяю технический анализ.", "completed")
        return self._run_workflow(state, progress)

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
        return {
            "status": "ok",
            "service": "mro-request-assessment",
            "dependencies": {
                "ata_impact": self.ata_client.safe_url,
                "mro_kb": self.mro_kb_client.safe_url,
                "similar_cases_fallback_enabled": self.allow_similar_cases_fallback,
            },
        }

    def _run_workflow(self, state: AssessmentState, progress: ProgressSink | None) -> AssessmentState:
        state.status = AssessmentStatus.ANALYZING
        state.decision = None
        state.capability_assessment = None
        state.approval_assessment = None
        state.mro_kb_evidence = []
        state.selected_similar_cases = []
        self.store.save(state)

        if state.workflow_iteration > 3:
            state.status = AssessmentStatus.WAITING_FOR_EXPERT
            state.decision = make_decision([], None, None, extra_review_reasons=[DecisionReason(code="CLARIFICATION_LIMIT_REACHED", source="clarification_manager", message="Maximum clarification iterations reached.")])
            self._emit(progress, state, "clarification_limit", "Достигнут предел трех циклов уточнения. Передаю на экспертную проверку.", "completed")
            return self.store.save(state)

        if not state.source.request_text.strip():
            state.status = AssessmentStatus.FAILED
            state.decision = make_decision([], None, None, extra_review_reasons=[DecisionReason(code="REQUEST_TEXT_MISSING", source="api", message="Request text is required.")])
            return self.store.save(state)

        if not self._call_ata_impact(state, progress):
            state.status = AssessmentStatus.WAITING_FOR_EXPERT
            state.decision = make_decision([], None, None, extra_review_reasons=[DecisionReason(code="ATA_IMPACT_UNAVAILABLE", source="mro-ata-impact", message="ATA Impact service is unavailable.")])
            return self.store.save(state)

        state.missing_information = assess_completeness(state)
        state.questions = build_questions(state.missing_information)
        if has_blocking_customer_missing(state.missing_information):
            state.status = AssessmentStatus.WAITING_FOR_INFORMATION
            state.decision = make_decision(state.missing_information, None, None)
            self._emit(progress, state, "completeness", "ATA Impact обнаружил блокирующие пробелы. Документальная проверка не выполняется.", "completed", details={"missing_count": len(state.missing_information)})
            return self.store.save(state)

        context = build_capability_context(state)
        self._emit(progress, state, "capability", "Проверяю базовое соответствие capability.", "started", details={"aircraft_family": context.aircraft_family, "ata": context.ata})
        capability = self.capability_provider.assess(context)
        state.capability_assessment = capability
        if capability.status.value == "FAIL":
            state.decision = make_decision([], capability, None)
            state.status = AssessmentStatus.DECISION_READY
            self._emit(progress, state, "capability", "Capability pre-check выявил подтвержденный hard fail. MRO RAG не вызывается.", "completed")
            return self.store.save(state)

        state.selected_similar_cases = select_similar_cases(state.similar_cases)
        document_review_required = self._maybe_call_mro_kb(state, progress)
        approval = assess_approval(context, capability)
        state.approval_assessment = approval
        self._emit(progress, state, "approval", "Проверяю маршрут одобрения.", "completed", details={"status": approval.status.value})
        state.decision = make_decision([], capability, approval, document_review_required=document_review_required)
        state.status = AssessmentStatus.WAITING_FOR_EXPERT if state.decision.status == AssessmentDecision.EXPERT_REVIEW else AssessmentStatus.DECISION_READY
        self._emit(progress, state, "decision", "Формирую предварительную рекомендацию.", "completed", details={"decision": state.decision.status.value})
        return self.store.save(state)

    def _call_ata_impact(self, state: AssessmentState, progress: ProgressSink | None) -> bool:
        fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
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
        self._emit(progress, state, "similar_cases_fallback", "Выполняю один fallback-вызов mro-similar-cases.", "started", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url)
        try:
            payload, trace = self.similar_cases_client.search(state.source.request_text, {"ata_impact": state.ata_impact or {}})
            state.external_call_trace.append(trace)  # type: ignore[arg-type]
            state.similar_cases = payload
            self._emit(progress, state, "similar_cases_fallback", "Fallback-поиск похожих заявок завершён.", "completed", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url, details=_similar_counts(payload))
        except ExternalServiceError as exc:
            state.external_call_trace.append(exc.trace)
            state.warnings.append("SIMILAR_CASES_FALLBACK_UNAVAILABLE")
            self._emit(progress, state, "similar_cases_fallback", "Fallback-поиск похожих заявок недоступен. Продолжаю без аналогов.", "failed", service="mro-similar-cases", safe_url=self.similar_cases_client.safe_url)

    def _maybe_call_mro_kb(self, state: AssessmentState, progress: ProgressSink | None) -> bool:
        if not _mro_kb_needed(state):
            self._emit(progress, state, "mro_kb_search", "Документальная проверка MRO RAG пропущена: результат не влияет на текущую рекомендацию.", "skipped", service="mro-kb")
            return False
        document_review_required = False
        if state.selected_similar_cases:
            for case in state.selected_similar_cases:
                query = build_case_query(case, state)
                self._emit(progress, state, "mro_kb_search", f"Обращаюсь к MRO RAG для документальной проверки {case.case_id}.", "started", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"case_id": case.case_id, "query": query})
                try:
                    payload, trace = self.mro_kb_client.chat(query)
                    state.external_call_trace.append(trace)  # type: ignore[arg-type]
                    state.mro_kb_evidence.extend(normalize_mro_kb_evidence(case.case_id, payload))
                    self._emit(progress, state, "mro_kb_search", "MRO RAG завершил проверку.", "completed", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"case_id": case.case_id, "sources": len(payload.get("sources") or []), "evidence_records": len(payload.get("evidence") or [])})
                except ExternalServiceError as exc:
                    state.external_call_trace.append(exc.trace)
                    state.warnings.append("DOCUMENTAL_VERIFICATION_UNAVAILABLE")
                    document_review_required = True
                    self._emit(progress, state, "mro_kb_search", "MRO RAG недоступен. Это не трактуется как отсутствие документов.", "failed", service="mro-kb", safe_url=self.mro_kb_client.safe_url)
        elif not qualified_cases(state.similar_cases):
            query = build_wide_query(state)
            self._emit(progress, state, "mro_kb_wide_search", "Qualified similar cases не найдены. Выполняю широкий документальный поиск.", "started", service="mro-kb", safe_url=self.mro_kb_client.safe_url, details={"query": query})
            try:
                payload, trace = self.mro_kb_client.chat(query)
                state.external_call_trace.append(trace)  # type: ignore[arg-type]
                state.mro_kb_evidence.extend(normalize_mro_kb_evidence(None, payload))
            except ExternalServiceError as exc:
                state.external_call_trace.append(exc.trace)
                state.warnings.append("DOCUMENTAL_VERIFICATION_UNAVAILABLE")
                document_review_required = True
        return document_review_required

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


def _apply_answer(state: AssessmentState, field: str, answer: Any) -> None:
    if field == "aircraft.msn":
        state.confirmed_inputs.msn = str(answer)
    elif field == "aircraft.registration":
        state.confirmed_inputs.registration = str(answer)
    elif field == "aircraft.aircraft_type":
        state.confirmed_inputs.aircraft_type = str(answer)
    elif field == "aircraft_model":
        state.confirmed_inputs.aircraft_model = str(answer)


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
    return str(sim.get("similarity_status") or "") == "no_qualified_matches" and bool((state.ata_impact or {}).get("affected_ata"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

