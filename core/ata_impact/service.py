from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Callable, Protocol

from core.runtime_clients import StructuredLLMResponse

from .certificate_validator import assess_certificate, validate_certificate
from .evidence import (
    AtaEvidenceRetriever,
    NullAtaEvidenceRetriever,
    is_controlled_evidence_document,
)
from .identifiers import extract_identifiers
from .modes import validate_ata_runtime_mode
from .models import MAPPING_CATEGORIES, empty_mapping
from .prompts import (
    ATA_CRITIC_PROMPT,
    ATA_JSON_REPAIR_PROMPT,
    ATA_MAPPING_PROMPT,
    ENGINEERING_FACT_EXTRACTION_PROMPT,
)
from .reference_catalog import AtaClassificationReferenceCatalog
from .schemas import (
    ATA_CRITIC_GENERATION_SCHEMA,
    ATA_MAPPING_GENERATION_SCHEMA,
    ENGINEERING_FACTS_SCHEMA,
)
from .validator import (
    apply_critic_additions,
    assemble,
    critical_ata_warning_reasons,
    validate_critic,
    validate_facts,
    validate_mapping,
)


class StructuredLLM(Protocol):
    def chat(self, system_prompt: str, user_prompt: str, allow_reasoning_fallback: bool = False) -> str:
        ...


class AtaImpactService:
    """Orchestrates logically separate ATA stages without semantic dictionaries."""

    def __init__(
        self,
        certificate: object,
        llm: StructuredLLM | None = None,
        evidence_retriever: AtaEvidenceRetriever | None = None,
        reference_catalog: AtaClassificationReferenceCatalog | None = None,
    ) -> None:
        self.certificate = certificate
        self.llm = llm
        self.evidence_retriever = evidence_retriever or NullAtaEvidenceRetriever()
        self.reference_catalog = (
            reference_catalog or AtaClassificationReferenceCatalog()
        )

    def analyze(
        self,
        request: str,
        fields: dict[str, object] | None = None,
        runtime_mode: str = "auto",
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        requested_mode = validate_ata_runtime_mode(runtime_mode)
        fields = fields if isinstance(fields, dict) else {}
        identifiers = extract_identifiers(request, fields)
        trace: list[dict[str, object]] = [
            {"step": "identifier_extraction", "status": "completed", "identifiers": identifiers}
        ]
        warnings: list[str] = []
        self._report(progress, "identifier_extraction", "Формальные идентификаторы извлечены.")

        if self.llm is None:
            return self._fallback(request, fields, identifiers, trace)

        facts_input = {"request": request, "fields": fields, "identifiers": identifiers}
        facts_payload, fact_response = self._call_json(
            ENGINEERING_FACT_EXTRACTION_PROMPT,
            facts_input,
            "engineering_fact_extraction",
            ENGINEERING_FACTS_SCHEMA,
        )
        facts, fact_warnings = validate_facts(facts_payload, identifiers)
        if (
            _has_schema_error(fact_warnings)
            and not fact_response.error
            and not fact_response.repair_attempted
        ):
            facts_payload, fact_response = self._repair_json(
                "engineering_fact_extraction",
                ENGINEERING_FACT_EXTRACTION_PROMPT,
                facts_input,
                ENGINEERING_FACTS_SCHEMA,
                fact_warnings,
            )
            facts, fact_warnings = validate_facts(facts_payload, identifiers)
        if _has_schema_error(fact_warnings) and fact_response.repair_attempted:
            fact_response = replace(
                fact_response,
                error="schema_validation_failed",
                repair_error=fact_response.repair_error or "schema_validation_failed",
                validation_errors=list(fact_warnings),
            )
        warnings.extend(fact_warnings)
        trace.append(self._structured_trace("engineering_fact_extraction", fact_response))
        if fact_response.error:
            return self._fallback(
                request,
                fields,
                identifiers,
                trace,
                [fact_response.error, *warnings],
            )
        if _has_schema_error(fact_warnings):
            return self._fallback(request, fields, identifiers, trace, fact_warnings, facts=facts)
        self._report(progress, "engineering_fact_extraction", "Инженерная модель заявки сформирована.")

        reference_context = self.reference_catalog.context(request, facts)
        if reference_context["status"] != "completed":
            warnings.append("classification_reference_unavailable")
        trace.append(
            {
                "step": "ata_classification_reference_retrieval",
                "status": reference_context["status"],
                "source": reference_context["source"],
                "section": reference_context["section"],
                "source_file_sha256": reference_context[
                    "source_file_sha256"
                ],
                "chapter_count": len(reference_context["chapter_index"]),
                "definition_count": len(
                    reference_context["relevant_definitions"]
                ),
                "retrieval_mode": reference_context.get("retrieval_mode"),
                "retrieved_reference_ids": list(
                    reference_context["retrieved_reference_ids"]
                ),
                "errors": list(reference_context["errors"]),
            }
        )
        self._report(
            progress,
            "ata_classification_reference_retrieval",
            "Релевантные определения ATA из ГОСТ подготовлены.",
        )

        mode = self._select_mode(requested_mode, identifiers, facts)
        required_affected_entities = _required_affected_entities(facts)
        mapping_input = {
            "request": request,
            "engineering_facts": facts,
            "relations": facts.get("relations", []),
            "required_affected_entities": required_affected_entities,
            "required_affected_entity_count": len(
                required_affected_entities
            ),
            "explicit_user_ata": identifiers["explicit_ata"],
            "ata_reference": {
                "source": reference_context["source"],
                "section": reference_context["section"],
                "revision": reference_context["revision"],
                "chapter_index": reference_context["chapter_index"],
                "relevant_definitions": reference_context[
                    "relevant_definitions"
                ],
            },
        }
        mapping_payload, mapping_response = self._call_json(
            ATA_MAPPING_PROMPT,
            mapping_input,
            "ata_mapping",
            ATA_MAPPING_GENERATION_SCHEMA,
        )
        mapping, mapping_warnings = validate_mapping(
            mapping_payload,
            facts,
            list(identifiers["explicit_ata"]),
            request,
        )
        if (
            _has_schema_error(mapping_warnings)
            and not mapping_response.error
            and not mapping_response.repair_attempted
        ):
            mapping_payload, mapping_response = self._repair_json(
                "ata_mapping",
                ATA_MAPPING_PROMPT,
                mapping_input,
                ATA_MAPPING_GENERATION_SCHEMA,
                mapping_warnings,
            )
            mapping, mapping_warnings = validate_mapping(
                mapping_payload,
                facts,
                list(identifiers["explicit_ata"]),
                request,
            )
        if _has_schema_error(mapping_warnings) and mapping_response.repair_attempted:
            mapping_response = replace(
                mapping_response,
                error="schema_validation_failed",
                repair_error=mapping_response.repair_error or "schema_validation_failed",
                validation_errors=list(mapping_warnings),
            )
        trace.append(self._structured_trace("ata_mapping", mapping_response, combined_runtime_call=False))

        if mapping_response.error:
            return self._fallback(
                request,
                fields,
                identifiers,
                trace,
                [mapping_response.error, *warnings],
                facts=facts,
                reference_context=reference_context,
            )

        warnings.extend(mapping_warnings)
        if _has_schema_error(mapping_warnings):
            return self._fallback(
                request,
                fields,
                identifiers,
                trace,
                mapping_warnings,
                facts=facts,
                reference_context=reference_context,
            )
        reference_mapping_payload = self.reference_catalog.propose_mapping_candidates(
            request,
            facts,
        )
        reference_mapping, reference_mapping_warnings = validate_mapping(
            reference_mapping_payload,
            facts,
            [],
            request,
        )
        warnings.extend(reference_mapping_warnings)
        reference_override_warnings = _apply_reference_mapping_owner(
            mapping,
            reference_mapping,
        )
        warnings.extend(reference_override_warnings)
        trace.append(
            {
                "step": "reference_mapping_owner",
                "status": "completed",
                "object_candidates": len(reference_mapping["object_ata"]),
                "structural_candidates": len(reference_mapping["structural_ata"]),
                "warnings": reference_override_warnings,
            }
        )
        warnings.extend(
            self.reference_catalog.enforce_mapping_roles(mapping)
        )
        synth_warnings = _synthesize_reference_candidates(
            mapping,
            facts,
            reference_context,
        )
        warnings.extend(synth_warnings)
        ungrounded_candidate_ids = set(
            self.reference_catalog.attach_references(mapping)
        )
        warnings.extend(
            f"classification_reference_missing:{candidate_id}"
            for candidate_id in sorted(ungrounded_candidate_ids)
        )
        certificate_validation = validate_certificate(self.certificate, _mapping_atas(mapping))
        trace.append({"step": "certificate_scope_validation", "status": "completed", "entries": len(certificate_validation)})
        self._report(progress, "certificate_scope_validation", "ATA отдельно проверены по области сертификата.")

        if requested_mode == "auto" and self._needs_post_mapping_extended(mapping, facts):
            mode = "extended"
        critic_reference = self.reference_catalog.context_for_atas(
            _mapping_atas(mapping)
        )
        required_candidate_ids = [
            str(item["candidate_id"])
            for category in MAPPING_CATEGORIES
            if category != "user_declared_ata"
            for item in mapping[category]
            if item.get("candidate_id")
        ]
        critic_input = {
            "request": request,
            "engineering_facts": facts,
            "relations": facts.get("relations", []),
            "ata_mapping": mapping,
            "required_candidate_ids": required_candidate_ids,
            "required_candidate_count": len(required_candidate_ids),
            "certificate_validation": certificate_validation,
            "ata_reference": critic_reference,
            "critic_depth": mode,
        }
        critic_payload, critic_response = self._call_json(
            ATA_CRITIC_PROMPT,
            critic_input,
            "independent_critic",
            ATA_CRITIC_GENERATION_SCHEMA,
        )
        critic_actions, critic_warnings = validate_critic(critic_payload, mapping, facts)
        if (
            _has_schema_error(critic_warnings)
            and not critic_response.error
            and not critic_response.repair_attempted
        ):
            critic_payload, critic_response = self._repair_json(
                "independent_critic",
                ATA_CRITIC_PROMPT,
                critic_input,
                ATA_CRITIC_GENERATION_SCHEMA,
                critic_warnings,
            )
            critic_actions, critic_warnings = validate_critic(critic_payload, mapping, facts)
        if _has_schema_error(critic_warnings) and critic_response.repair_attempted:
            critic_response = replace(
                critic_response,
                error="schema_validation_failed",
                repair_error=critic_response.repair_error or "schema_validation_failed",
                validation_errors=list(critic_warnings),
            )
        trace.append(
            self._structured_trace(
                "independent_critic",
                critic_response,
                combined_runtime_call=False,
            )
        )
        if critic_response.error:
            warnings.append(critic_response.error)
        critic_actions, critic_warnings = _apply_reference_owned_critic_actions(
            mapping,
            critic_actions,
            critic_warnings,
        )
        warnings.extend(critic_warnings)
        critic_actions = _remove_ungrounded_confirms(
            critic_actions,
            mapping,
            ungrounded_candidate_ids,
        )
        mapping, critic_actions, addition_warnings = apply_critic_additions(mapping, critic_actions, facts, request)
        warnings.extend(addition_warnings)
        added_ungrounded = set(self.reference_catalog.attach_references(mapping))
        warnings.extend(
            f"classification_reference_missing:{candidate_id}"
            for candidate_id in sorted(added_ungrounded - ungrounded_candidate_ids)
        )
        # Critic additions are validated and then receive the same deterministic
        # certificate/document processing as mapper candidates.
        certificate_validation = validate_certificate(self.certificate, _mapping_atas(mapping))
        self._report(progress, "independent_critic", "Критик проверил роли и основания ATA.")

        document_types = ["AMM", "SRM", "IPC", "CMM", "WDM", "NTM", "ALS", "approved_repair_data"]
        evidence_candidates = [
            {
                **item,
                "mapping_category": category,
            }
            for category in MAPPING_CATEGORIES
            if category not in {"location_context_ata", "user_declared_ata"}
            for item in mapping[category]
            if item.get("candidate_id")
        ]
        try:
            evidence = self.evidence_retriever.search(
                request=request,
                engineering_facts=facts,
                ata_candidates=_mapping_atas(mapping),
                mapping_candidates=evidence_candidates,
                aircraft=facts.get("aircraft", {}) if isinstance(facts.get("aircraft"), dict) else {},
                document_types=document_types,
                limit=12,
            ).as_dict()
        except Exception:
            evidence = {"status": "error", "documents": [], "warnings": ["OEM evidence retrieval failed"]}
        trace.append(
            {
                "step": "oem_document_verification",
                "status": evidence["status"],
                "document_count": len(evidence["documents"]),
                "warnings": evidence["warnings"],
            }
        )
        assembled = assemble(mapping, critic_actions, certificate_validation, evidence)
        certificate_assessment = assess_certificate(
            self.certificate,
            list(assembled["affected_ata"]),
            list(assembled["potentially_affected_ata"]),
        )
        required = self._required_inputs(
            facts,
            bool(assembled["affected_ata"]),
        )
        valid_candidates = {
            str(item.get("candidate_id")): {
                **item,
                "mapping_category": category,
            }
            for category in MAPPING_CATEGORIES
            for item in mapping[category]
            if item.get("candidate_id")
        }
        retrieved_documents = [
            item for item in evidence.get("documents", []) if isinstance(item, dict)
        ]
        controlled_evidence = [
            item
            for item in retrieved_documents
            if is_controlled_evidence_document(item, valid_candidates)
        ]
        all_warnings = list(
            dict.fromkeys(
                [*warnings, *[str(item) for item in evidence.get("warnings", [])]]
            )
        )
        validation_reasons = critical_ata_warning_reasons(all_warnings)
        validation_gate = {
            "critical": bool(validation_reasons),
            "reasons": validation_reasons,
        }
        decision, decision_reasons = self._decision(
            assembled,
            facts,
            required,
            validation_gate,
            evidence,
            certificate_assessment,
        )
        result = {
            "agent": "ata_impact",
            "contract_version": "v2",
            "runtime_mode": mode,
            "input": {"request": request, "aircraft_type": fields.get("aircraft_type"), "fields": fields},
            "identifiers": identifiers,
            "engineering_facts": facts,
            "ata_mapping": mapping,
            "critic_actions": critic_actions,
            "classification_reference": _reference_summary(reference_context),
            "certificate_validation": certificate_validation,
            "certificate_assessment": certificate_assessment,
            "certificate_catalog_available": bool(getattr(self.certificate, "entries", [])),
            **assembled,
            "required_input_data": required,
            "document_verification": evidence,
            "retrieved_documents": retrieved_documents,
            "controlled_evidence": controlled_evidence,
            "decision": decision,
            "decision_reasons": decision_reasons,
            "validation_gate": validation_gate,
            "needs_human_approval": True,
            "agent_trace": trace,
            "warnings": all_warnings,
            "capability_screening": "not_assessed",
        }
        self._add_compatibility(result, identifiers)
        result["answer"] = self._answer(result)
        self._report(progress, "completed", "Предварительная оценка ATA сформирована.")
        return result

    def _fallback(
        self,
        request: str,
        fields: dict[str, object],
        identifiers: dict[str, object],
        trace: list[dict[str, object]],
        warnings: list[str] | None = None,
        facts: dict[str, object] | None = None,
        reference_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        mapping = empty_mapping()
        mapping["user_declared_ata"] = [
            {
                "candidate_id": (
                    "candidate:user_declared_ata:request:"
                    f"{ata.replace(' ', '_').replace('-', '_')}:{sequence}"
                ),
                "initial_state": "candidate_unverified",
                "ata": ata,
                "confidence": 1.0,
                "declared_assessment": "unverified",
                "reason": "Explicitly declared ATA; semantic LLM analysis unavailable",
                "source_fragment": ata,
            }
            for sequence, ata in enumerate(identifiers["explicit_ata"], start=1)
        ]
        if facts is None:
            facts, fact_warnings = validate_facts({}, identifiers)
        else:
            fact_warnings = []
        certificate_validation = validate_certificate(self.certificate, list(identifiers["explicit_ata"]))
        evidence = NullAtaEvidenceRetriever().search(
            request,
            facts,
            list(identifiers["explicit_ata"]),
            list(mapping["user_declared_ata"]),
            facts["aircraft"],
            [],
            0,
        ).as_dict()  # type: ignore[arg-type]
        assembled = assemble(mapping, [], certificate_validation, evidence)
        certificate_assessment = assess_certificate(
            self.certificate,
            list(assembled["affected_ata"]),
            list(assembled["potentially_affected_ata"]),
        )
        trace.append(
            {
                "step": "fallback",
                "status": "engineering_review_required",
                "mechanism": "explicit_ata_only",
            }
        )
        result = {
            "agent": "ata_impact",
            "contract_version": "v2",
            "runtime_mode": "fallback",
            "input": {"request": request, "aircraft_type": fields.get("aircraft_type"), "fields": fields},
            "identifiers": identifiers,
            "engineering_facts": facts,
            "ata_mapping": mapping,
            "critic_actions": [],
            "classification_reference": _reference_summary(
                reference_context
                if isinstance(reference_context, dict)
                else self.reference_catalog.context(request, facts)
            ),
            "certificate_validation": certificate_validation,
            "certificate_assessment": certificate_assessment,
            "certificate_catalog_available": bool(getattr(self.certificate, "entries", [])),
            **assembled,
            "required_input_data": list(
                dict.fromkeys(
                    [
                        *self._required_inputs(
                            facts,
                            False,
                        ),
                        "engineering semantic classification by LLM or engineer",
                    ]
                )
            ),
            "document_verification": evidence,
            "retrieved_documents": [],
            "controlled_evidence": [],
            "decision": "engineering_review_required",
            "decision_reasons": ["semantic_analysis_unavailable"],
            "validation_gate": {
                "critical": True,
                "reasons": ["semantic_analysis_unavailable"],
            },
            "needs_human_approval": True,
            "agent_trace": trace,
            "warnings": list(dict.fromkeys([*(warnings or []), *fact_warnings, "llm_unavailable_or_invalid", *evidence["warnings"]])),
            "capability_screening": "not_assessed",
        }
        self._add_compatibility(result, identifiers)
        result["answer"] = self._answer(result)
        return result

    def _call_json(
        self,
        system: str,
        payload: dict[str, object],
        stage: str,
        schema: dict[str, object],
    ) -> tuple[dict[str, object], StructuredLLMResponse]:
        if self.llm is None:
            return {}, self._empty_structured_response("llm_unavailable")
        response = self._structured_request(stage, system, payload, schema)
        if response.finish_reason == "length":
            return {}, replace(
                response,
                error="truncated_response",
                primary_error=response.primary_error or "truncated_response",
            )
        if response.error and response.error.startswith(
            ("transport_error:", "unsupported_structured_output_mode:")
        ):
            return {}, response
        try:
            if response.error:
                raise ValueError(response.error)
            value = response.parsed
            if value is None:
                value, _ = _parse_llm_response(response.content)
            if not isinstance(value, dict):
                raise ValueError("llm_response_not_object")
            return _drop_optional_null_properties(value, schema), response
        except Exception as exc:
            primary_error = (
                response.primary_error
                or response.error
                or str(exc)
                or f"llm_stage_failed:{type(exc).__name__}"
            )
            validation_errors = list(response.validation_errors or [])
            repair_errors = validation_errors or [primary_error]
            repaired_payload, repair_response = self._repair_json(
                stage,
                system,
                payload,
                schema,
                repair_errors,
            )
            return repaired_payload, replace(
                repair_response,
                primary_error=primary_error,
                validation_errors=validation_errors,
            )

    def _repair_json(
        self,
        stage: str,
        original_system: str,
        payload: dict[str, object],
        schema: dict[str, object],
        errors: list[str],
    ) -> tuple[dict[str, object], StructuredLLMResponse]:
        if self.llm is None:
            return {}, replace(
                self._empty_structured_response("llm_unavailable"),
                repair_attempted=True,
            )
        repair_payload = {
            "stage": stage,
            "validation_errors": errors,
            "stage_contract": original_system,
            "response_schema": schema,
            "original_input": payload,
        }
        response = replace(
            self._structured_request(
                "json_repair",
                ATA_JSON_REPAIR_PROMPT,
                repair_payload,
                schema,
            ),
            repair_attempted=True,
        )
        if response.finish_reason == "length":
            return {}, replace(
                response,
                error="truncated_response",
                repair_error=response.repair_error or "truncated_response",
            )
        try:
            if response.error:
                raise ValueError(response.error)
            value = response.parsed
            if value is None:
                value, _ = _parse_llm_response(response.content)
            if not isinstance(value, dict):
                raise ValueError("llm_response_not_object")
            return _drop_optional_null_properties(value, schema), response
        except Exception as exc:
            error = str(exc) or f"repair_failed:{type(exc).__name__}"
            return {}, replace(
                response,
                error=error,
                repair_error=response.repair_error or error,
            )

    def _structured_request(
        self,
        stage: str,
        system: str,
        payload: dict[str, object],
        schema: dict[str, object],
    ) -> StructuredLLMResponse:
        if self.llm is None:
            return self._empty_structured_response("llm_unavailable")
        structured = getattr(self.llm, "structured_chat", None)
        if callable(structured):
            try:
                response = structured(
                    stage=stage,
                    system_prompt=system,
                    input_payload=payload,
                    response_schema=schema,
                )
                if isinstance(response, StructuredLLMResponse):
                    return response
                return self._empty_structured_response(
                    "invalid_structured_transport_response"
                )
            except Exception as exc:
                return self._empty_structured_response(
                    f"transport_error:{type(exc).__name__}"
                )
        try:
            raw = self._chat(system, payload)
            parsed = raw.get("parsed") if isinstance(raw, dict) else None
            content = (
                str(raw.get("content") or "")
                if isinstance(raw, dict)
                else str(raw or "")
            )
            finish_reason = _response_finish_reason(raw)
            error = None
            if not content.strip() and not isinstance(parsed, dict):
                error = "empty_content"
            return StructuredLLMResponse(
                parsed=parsed if isinstance(parsed, dict) else None,
                content=content,
                finish_reason=finish_reason,
                structured_output_mode="prompt_only",
                schema_enforced=False,
                error=error,
            )
        except Exception as exc:
            return self._empty_structured_response(
                f"transport_error:{type(exc).__name__}"
            )

    @staticmethod
    def _empty_structured_response(error: str) -> StructuredLLMResponse:
        return StructuredLLMResponse(
            parsed=None,
            content="",
            finish_reason=None,
            structured_output_mode="unavailable",
            schema_enforced=False,
            schema_enforcement_requested=False,
            server_profile_accepted=False,
            local_schema_valid=False,
            error=error,
            primary_error=error,
        )

    @staticmethod
    def _structured_trace(
        stage: str,
        response: StructuredLLMResponse,
        **extra: object,
    ) -> dict[str, object]:
        return {
            "step": stage,
            "status": (
                "repair_failed"
                if response.error
                and (
                    response.repair_attempted
                    or response.error == "truncated_response"
                )
                else "error"
                if response.error
                else "completed"
            ),
            "reason": response.error,
            "warning": response.error,
            "repair": (
                "completed"
                if response.repair_attempted and not response.error
                else "repair_failed"
                if response.repair_attempted
                or response.error == "truncated_response"
                else "not_needed"
            ),
            "finish_reason": response.finish_reason,
            "requested_structured_output_mode": response.requested_structured_output_mode,
            "structured_output_mode": response.structured_output_mode,
            "schema_enforcement_requested": response.schema_enforcement_requested,
            "server_profile_accepted": response.server_profile_accepted,
            "local_schema_valid": response.local_schema_valid,
            "schema_enforced_by_server": response.schema_enforced,
            "primary_error": response.primary_error,
            "repair_error": response.repair_error,
            "validation_errors": list(response.validation_errors or []),
            "latency_ms": round(response.latency_ms, 3),
            **extra,
        }

    def _chat(self, system: str, payload: dict[str, object]) -> object:
        if self.llm is None:
            raise RuntimeError("llm_unavailable")
        structured = getattr(self.llm, "chat_response", None)
        if callable(structured):
            return structured(
                system,
                json.dumps(payload, ensure_ascii=False),
                allow_reasoning_fallback=False,
            )
        return self.llm.chat(
            system,
            json.dumps(payload, ensure_ascii=False),
            allow_reasoning_fallback=False,
        )

    @staticmethod
    def _select_mode(requested: str, identifiers: dict[str, object], facts: dict[str, object]) -> str:
        if requested in {"standard", "extended"}:
            return requested
        objects = facts.get("physical_objects", [])
        relations = facts.get("relations", [])
        uncertain = facts.get("uncertainties", [])
        complex_event = str((facts.get("event") or {}).get("type") or "") in {"modification", "software_change"} if isinstance(facts.get("event"), dict) else False
        engineering_relations = [
            item
            for item in relations if isinstance(item, dict)
            and item.get("relation") not in {"location_reference", "adjacent_to", "installed_in"}
        ] if isinstance(relations, list) else []
        maintenance_action = str((facts.get("event") or {}).get("maintenance_action") or "") if isinstance(facts.get("event"), dict) else ""
        if (
            identifiers.get("ad_references")
            or identifiers.get("sb_references")
            or len(objects if isinstance(objects, list) else []) > 1
            or bool(engineering_relations)
            or len(uncertain if isinstance(uncertain, list) else []) > 1
            or complex_event
            or "modif" in maintenance_action.lower()
        ):
            return "extended"
        return "standard"

    @staticmethod
    def _needs_post_mapping_extended(
        mapping: dict[str, list[dict[str, object]]],
        facts: dict[str, object],
    ) -> bool:
        potential_count = len(mapping["interface_ata_hypotheses"]) + len(mapping["procedure_ata_hypotheses"])
        conflict = any(
            item.get("declared_assessment") == "conflicting"
            for item in mapping["user_declared_ata"]
        )
        confidences = [
            float(item.get("confidence") or 0.0)
            for category in MAPPING_CATEGORIES
            for item in mapping[category]
        ]
        low_confidence = bool(confidences) and min(confidences) < 0.5
        avionics = any(
            str(item.get("relation") or "") == "connected_to"
            for item in facts.get("relations", [])
            if isinstance(item, dict)
        )
        return potential_count > 0 or conflict or low_confidence or avionics

    @staticmethod
    def _required_inputs(
        facts: dict[str, object],
        has_affected: bool,
    ) -> list[str]:
        missing: list[str] = []
        aircraft = facts.get("aircraft", {})
        if not isinstance(aircraft, dict) or not (aircraft.get("family") or aircraft.get("model")):
            missing.append("тип ВС и effectivity/MSN")
        if not has_affected:
            missing.append("точный повреждённый или изменяемый объект")
        if facts.get("uncertainties"):
            missing.append("уточнение инженерных неопределённостей из модели заявки")
        return missing

    @staticmethod
    def _decision(
        assembled: dict[str, object],
        facts: dict[str, object],
        required: list[str],
        validation_gate: dict[str, object],
        evidence: dict[str, object],
        certificate_assessment: dict[str, object],
    ) -> tuple[str, list[str]]:
        validated = assembled["validated_ata"]
        reasons: list[str] = []
        if not assembled["affected_ata"]:
            reasons.append("affected_ata_empty")
        if assembled["potentially_affected_ata"]:
            reasons.append("potential_ata_open")
        if validated["document_verification_required"]:
            reasons.append("document_verification_required")
        if validated["candidate_unverified"]:
            reasons.append("critic_coverage_incomplete")
        if (
            validated["user_declared_unverified"]
            or validated["user_declared_conflicting"]
            or validated["user_declared_not_in_certificate"]
        ):
            reasons.append("user_declared_ata_unresolved")
        if required:
            reasons.append("required_input_data_open")
        if facts.get("uncertainties"):
            reasons.append("engineering_uncertainties_open")
        if str(evidence.get("status") or "") != "completed":
            reasons.append("evidence_stage_incomplete")
        certificate_status = str(certificate_assessment.get("status") or "")
        if certificate_status == "catalog_unavailable":
            reasons.append("certificate_catalog_unavailable")
        if certificate_status in {"partially_covered", "not_covered"}:
            reasons.append("certificate_scope_review_required")
        if validation_gate.get("critical") is True:
            reasons.append("critical_validation_warning")
        reasons = list(dict.fromkeys(reasons))
        if not reasons:
            return "completed", []
        if "document_verification_required" in reasons:
            return "document_verification_required", reasons
        if (
            "critic_coverage_incomplete" in reasons
            or "user_declared_ata_unresolved" in reasons
            or "critical_validation_warning" in reasons
        ):
            return "engineering_review_required", reasons
        if "required_input_data_open" in reasons or "engineering_uncertainties_open" in reasons:
            return "additional_input_required", reasons
        hypotheses_only = set(reasons) <= {"potential_ata_open"}
        if assembled["affected_ata"] and hypotheses_only:
            return "completed_with_hypotheses", reasons
        return "engineering_review_required", reasons

    @staticmethod
    def _add_compatibility(result: dict[str, object], identifiers: dict[str, object]) -> None:
        validated = result["validated_ata"]
        affected = list(result["affected_ata"])
        direct_system = [
            {**item, "role": "direct_system", "deprecated": True}
            for status in ("direct_confirmed", "inferred_from_request", "document_confirmed")
            for item in validated[status]
            if item.get("mapping_category") == "object_ata"
        ]
        direct_structural = [
            {**item, "role": "direct_structural", "deprecated": True}
            for status in ("direct_confirmed", "inferred_from_request", "document_confirmed")
            for item in validated[status]
            if item.get("mapping_category") == "structural_ata"
        ]
        secondary = [
            {
                **item,
                "legacy_role": "hypothesis",
                "deprecated": True,
            }
            for status in (
                "possible_interface",
                "possible_procedure",
                "document_verification_required",
                "candidate_unverified",
            )
            for item in validated[status]
        ]
        certificate_scope = _legacy_certificate_scope(
            result.get("certificate_validation"),
            bool(result.get("certificate_catalog_available")),
        )
        result.update(
            {
                "extracted_facts": result["engineering_facts"],
                "mode": result["runtime_mode"],
                "direct_system_ata": direct_system,
                "direct_structural_ata": direct_structural,
                "secondary_ata_hypotheses": secondary,
                "procedure_references": identifiers.get("document_references", []),
                "direct_ata": affected,
                "secondary_ata": secondary,
                "confirmed_affected_ata": affected,
                "certificate_scope": certificate_scope,
                "certificate_chapter_match": certificate_scope,
                "controlled_evidence": list(result.get("controlled_evidence") or []),
                "internet_context": [],
                "ontology_version": "deprecated-disabled",
                "provenance": {
                    "semantic_classifier": "unavailable" if result["runtime_mode"] == "fallback" else "llm",
                    "legacy_ontology": "disabled",
                    "classification_reference": "gost_18675_2012_appendix_a",
                    "certificate": "scope_validation_only",
                },
                "compatibility": {"version": "v1", "deprecated_fields": True},
            }
        )

    @staticmethod
    def _answer(result: dict[str, object]) -> str:
        assessment = result.get("certificate_assessment")
        assessment_status = (
            str(assessment.get("status") or "undetermined")
            if isinstance(assessment, dict)
            else "undetermined"
        )
        certificate_labels = {
            "covered": "область найдена",
            "partially_covered": "область найдена частично",
            "not_covered": "область отсутствует",
            "undetermined": "невозможно определить до подтверждения ATA",
            "catalog_unavailable": "каталог недоступен",
        }
        lines = [
            "## Предварительная оценка ATA",
            "",
            f"- Сертификат: {certificate_labels.get(assessment_status, assessment_status)}",
            "- Совпадение с сертификатом не является окончательным capability approval.",
        ]
        for key, label in (
            ("affected_ata", "Затронутые ATA"),
            ("potentially_affected_ata", "Потенциально затронутые ATA"),
            ("context_ata", "ATA местоположения/контекста"),
        ):
            values = result.get(key, [])
            if values:
                lines.append(f"- {label}: " + ", ".join(str(value) for value in values))
        lines.extend(
            [
                f"- Решение: {result.get('decision')}",
                "- Требуется инженерное подтверждение: да",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _report(progress: Callable[[dict[str, object]], None] | None, stage: str, message: str) -> None:
        if progress:
            progress({"stage": stage, "message": message})


def _mapping_atas(mapping: dict[str, list[dict[str, object]]]) -> list[str]:
    return sorted({str(item["ata"]) for category in MAPPING_CATEGORIES for item in mapping[category] if item.get("ata")})


def _apply_reference_mapping_owner(
    mapping: dict[str, list[dict[str, object]]],
    reference_mapping: dict[str, list[dict[str, object]]],
) -> list[str]:
    warnings: list[str] = []
    for category in ("object_ata", "structural_ata"):
        replacements = {
            str(item.get("entity_id") or ""): item
            for item in reference_mapping.get(category, [])
            if (
                isinstance(item, dict)
                and item.get("entity_id")
                and item.get("ata")
                and float(item.get("confidence") or 0.0) >= 0.65
                and "controlled_reference_hint" in str(item.get("reason") or "")
            )
        }
        if not replacements:
            continue
        kept: list[dict[str, object]] = []
        replaced_ids: set[str] = set()
        for item in mapping.get(category, []):
            entity_id = str(item.get("entity_id") or "")
            replacement = replacements.get(entity_id)
            if replacement is None:
                kept.append(item)
                continue
            if item.get("ata") == replacement.get("ata"):
                kept.append({**item, "mapping_source": "gost_reference_ranker"})
                replaced_ids.add(entity_id)
                continue
            kept.append(
                {
                    **replacement,
                    "candidate_id": item.get("candidate_id"),
                    "initial_state": item.get("initial_state", "candidate_unverified"),
                    "mapping_source": "gost_reference_ranker",
                    "previous_llm_ata": item.get("ata"),
                    "previous_llm_candidate_id": item.get("candidate_id"),
                }
            )
            replaced_ids.add(entity_id)
            if item.get("ata") != replacement.get("ata"):
                warnings.append(
                    "reference_mapping_replaced_llm_candidate:"
                    f"{category}:{entity_id}:{item.get('ata')}->{replacement.get('ata')}"
                )
        for entity_id, replacement in replacements.items():
            if entity_id in replaced_ids:
                continue
            kept.append({**replacement, "mapping_source": "gost_reference_ranker"})
            warnings.append(
                "reference_mapping_added_candidate:"
                f"{category}:{entity_id}:{replacement.get('ata')}"
            )
        mapping[category] = kept
    return warnings


def _apply_reference_owned_critic_actions(
    mapping: dict[str, list[dict[str, object]]],
    critic_actions: list[dict[str, object]],
    critic_warnings: list[str],
) -> tuple[list[dict[str, object]], list[str]]:
    reference_owned: dict[str, tuple[str, dict[str, object]]] = {
        str(item.get("candidate_id")): (category, item)
        for category in ("object_ata", "structural_ata")
        for item in mapping.get(category, [])
        if isinstance(item, dict)
        and item.get("candidate_id")
        and item.get("mapping_source") == "gost_reference_ranker"
    }
    if not reference_owned:
        return critic_actions, critic_warnings
    filtered_actions = [
        action
        for action in critic_actions
        if str(action.get("candidate_id") or "") not in reference_owned
    ]
    filtered_warnings = [
        warning
        for warning in critic_warnings
        if not any(str(warning).endswith(candidate_id) for candidate_id in reference_owned)
    ]
    for candidate_id, (category, item) in reference_owned.items():
        filtered_actions.append(
            {
                "candidate_id": candidate_id,
                "action": "confirm",
                "reason": "Confirmed by deterministic GOST reference ranker before LLM critic.",
                "ata": item.get("ata"),
                "category": category,
                "entity_id": item.get("entity_id"),
                "relation_id": item.get("relation_id"),
            }
        )
    return filtered_actions, filtered_warnings


def _remove_ungrounded_confirms(
    actions: list[dict[str, object]],
    mapping: dict[str, list[dict[str, object]]],
    ungrounded_candidate_ids: set[str],
) -> list[dict[str, object]]:
    affected_candidates = {
        str(candidate.get("candidate_id") or "")
        for category in ("object_ata", "structural_ata")
        for candidate in mapping[category]
    }
    return [
        action
        for action in actions
        if not (
            action.get("action") == "confirm"
            and str(action.get("candidate_id") or "")
            in ungrounded_candidate_ids
            and str(action.get("candidate_id") or "") in affected_candidates
        )
    ]


def _reference_summary(context: dict[str, object]) -> dict[str, object]:
    return {
        "status": context.get("status"),
        "source": context.get("source"),
        "section": context.get("section"),
        "revision": context.get("revision"),
        "source_file_sha256": context.get("source_file_sha256"),
        "retrieval_mode": context.get("retrieval_mode"),
        "chapter_count": len(context.get("chapter_index") or []),
        "definition_count": len(
            context.get("relevant_definitions") or []
        ),
        "retrieved_reference_ids": list(
            context.get("retrieved_reference_ids") or []
        ),
        "errors": list(context.get("errors") or []),
    }


def _synthesize_reference_candidates(
    mapping: dict[str, list[dict[str, object]]],
    facts: dict[str, object],
    reference_context: dict[str, object],
) -> list[str]:
    chapter_index = reference_context.get("chapter_index")
    if not isinstance(chapter_index, list):
        return []
    existing_ids = {
        str(item.get("entity_id") or "")
        for category in ("object_ata", "structural_ata")
        for item in mapping.get(category, [])
        if isinstance(item, dict)
    }
    warnings: list[str] = []
    for entity in _required_affected_entities(facts):
        entity_id = str(entity.get("entity_id") or "")
        entity_type = str(entity.get("entity_type") or "")
        if not entity_id or entity_id in existing_ids:
            continue
        category = (
            "structural_ata"
            if entity_type == "structure"
            else "object_ata"
        )
        ata = _first_allowed_reference_chapter(
            chapter_index,
            category,
        )
        if not ata:
            continue
        candidate = {
            "candidate_id": _next_candidate_id(
                mapping,
                category,
                entity_id,
                ata,
            ),
            "initial_state": "candidate_unverified",
            "ata": ata,
            "confidence": 0.55,
            "reason": (
                "Deterministic GOST shortlist fallback for affected "
                f"{entity_type}"
            ),
            "entity_id": entity_id,
            "source_fragment": str(entity.get("name") or ""),
        }
        mapping[category].append(candidate)
        existing_ids.add(entity_id)
        warnings.append(
            f"reference_shortlist_candidate_added:{category}:{entity_id}:{ata}"
        )
    return warnings


def _required_affected_entities(
    facts: dict[str, object],
) -> list[dict[str, str]]:
    damaged_ids = {
        str(item.get("affected_entity_id"))
        for item in facts.get("damage", [])
        if isinstance(item, dict) and item.get("affected_entity_id")
    }
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entity_type in (
        ("physical_objects", "object"),
        ("structural_elements", "structure"),
    ):
        for item in facts.get(key, []):
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("id") or "")
            involvement = str(item.get("involvement") or "")
            affected = (
                entity_id in damaged_ids
                or item.get("damage_confirmed") is True
                or involvement.lower()
                in {
                    "damaged",
                    "changed",
                    "modified",
                    "removed",
                    "replaced",
                    "repair",
                    "repaired",
                    "work_target",
                }
            )
            identity = (entity_type, entity_id)
            if not entity_id or not affected or identity in seen:
                continue
            seen.add(identity)
            result.append(
                {
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "name": str(item.get("name") or ""),
                    "involvement": involvement,
                }
            )
    return result


def _first_allowed_reference_chapter(
    chapter_index: list[object],
    category: str,
) -> str:
    for item in chapter_index:
        if not isinstance(item, dict):
            continue
        ata = str(item.get("ata") or "")
        allowed = item.get("allowed_mapping_categories")
        if (
            ata
            and isinstance(allowed, list)
            and category in {str(value) for value in allowed}
        ):
            return ata
    return ""


def _next_candidate_id(
    mapping: dict[str, list[dict[str, object]]],
    category: str,
    anchor: str,
    ata: str,
) -> str:
    existing = {
        str(item.get("candidate_id") or "")
        for mapping_category in MAPPING_CATEGORIES
        for item in mapping.get(mapping_category, [])
        if isinstance(item, dict) and item.get("candidate_id")
    }
    sequence = 1
    anchor_token = _candidate_token(anchor)
    ata_token = _candidate_token(ata)
    candidate_id = (
        f"candidate:{category}:{anchor_token}:{ata_token}:{sequence}"
    )
    while candidate_id in existing:
        sequence += 1
        candidate_id = (
            f"candidate:{category}:{anchor_token}:{ata_token}:{sequence}"
        )
    return candidate_id


def _candidate_token(value: object) -> str:
    token = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(value or "").lower(),
    ).strip("_")
    return token or "request"


def _legacy_certificate_scope(
    validation: object,
    catalog_loaded: bool,
) -> dict[str, object]:
    entries = [
        item for item in (validation or []) if isinstance(item, dict)
    ]
    matched = [
        {
            "ata": item.get("ata"),
            "certificate_ata": item.get("certificate_ata"),
            "match_type": item.get("match_type"),
            "name": (item.get("certificate_entry") or {}).get("name", "")
            if isinstance(item.get("certificate_entry"), dict)
            else "",
        }
        for item in entries
        if item.get("certificate_scope_status") == "in_scope_candidate"
    ]
    unmatched = [
        str(item.get("ata"))
        for item in entries
        if item.get("certificate_scope_status") == "not_in_certificate"
    ]
    ambiguous = [
        str(item.get("ata"))
        for item in entries
        if item.get("certificate_scope_status") == "ambiguous_subchapter"
    ]
    statuses = {
        str(item.get("certificate_scope_status")) for item in entries
    }
    status = (
        "catalog_unavailable"
        if not catalog_loaded or "catalog_unavailable" in statuses
        else "out_of_scope"
        if unmatched
        else "ambiguous"
        if ambiguous
        else "in_scope_candidate"
        if entries
        else "unknown"
    )
    return {
        "status": status,
        "catalog_loaded": catalog_loaded,
        "matched": matched,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "capability_approval": False,
        "deprecated": True,
    }


def _certificate_summary(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "unknown"
    statuses = {str(item.get("certificate_scope_status")) for item in items if isinstance(item, dict)}
    if "catalog_unavailable" in statuses:
        return "catalog_unavailable"
    if "not_in_certificate" in statuses:
        return "out_of_scope"
    if "ambiguous_subchapter" in statuses:
        return "ambiguous"
    return "in_scope_candidate"


def _has_schema_error(warnings: list[str]) -> bool:
    return any(
        warning.startswith(
            (
                "schema_",
                "duplicate_entity_id",
                "duplicate_relation_id",
                "duplicate_candidate_id:",
                "mapping_item_missing_required:",
                "invalid_critic_action",
                "invalid_candidate_id_format:",
                "invalid_ata:",
                "critic_action_missing_reason:",
            )
        )
        for warning in warnings
    )


def _drop_optional_null_properties(
    value: object,
    schema: dict[str, object],
) -> object:
    if isinstance(value, dict):
        required = {
            str(item)
            for item in schema.get("required", [])
            if isinstance(item, str)
        }
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        return {
            key: _drop_optional_null_properties(
                item,
                properties.get(key)
                if isinstance(properties.get(key), dict)
                else {},
            )
            for key, item in value.items()
            if item is not None or key in required
        }
    if isinstance(value, list):
        item_schema = (
            schema.get("items")
            if isinstance(schema.get("items"), dict)
            else {}
        )
        return [
            _drop_optional_null_properties(item, item_schema)
            for item in value
        ]
    return value


def _parse_llm_response(raw: object) -> tuple[dict[str, object], str | None]:
    finish_reason: str | None = None
    if isinstance(raw, dict):
        finish_reason = str(raw.get("finish_reason") or "") or None
        parsed = raw.get("parsed")
        if isinstance(parsed, dict):
            return parsed, finish_reason
        if "content" in raw:
            raw = raw.get("content")
        else:
            return dict(raw), finish_reason
    if not isinstance(raw, str):
        raise ValueError("llm_response_not_object")
    stripped = raw.strip()
    if stripped.startswith("```"):
        import re

        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
        if fenced is None:
            raise ValueError("invalid_fenced_json")
        stripped = fenced.group(1).strip()
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("llm_response_not_object")
    return value, finish_reason


def _response_finish_reason(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    return str(raw.get("finish_reason") or "") or None
