from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, Protocol

from core.runtime_clients import StructuredLLMResponse

from .certificate_validator import validate_certificate
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
from .schemas import ATA_CRITIC_SCHEMA, ATA_MAPPING_SCHEMA, ENGINEERING_FACTS_SCHEMA
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
    ) -> None:
        self.certificate = certificate
        self.llm = llm
        self.evidence_retriever = evidence_retriever or NullAtaEvidenceRetriever()

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
            fact_response = replace(fact_response, error="schema_validation_failed")
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

        mode = self._select_mode(requested_mode, identifiers, facts)
        mapping_input = {
            "request": request,
            "engineering_facts": facts,
            "relations": facts.get("relations", []),
            "explicit_user_ata": identifiers["explicit_ata"],
        }
        mapping_payload, mapping_response = self._call_json(
            ATA_MAPPING_PROMPT,
            mapping_input,
            "ata_mapping",
            ATA_MAPPING_SCHEMA,
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
                ATA_MAPPING_SCHEMA,
                mapping_warnings,
            )
            mapping, mapping_warnings = validate_mapping(
                mapping_payload,
                facts,
                list(identifiers["explicit_ata"]),
                request,
            )
        if _has_schema_error(mapping_warnings) and mapping_response.repair_attempted:
            mapping_response = replace(mapping_response, error="schema_validation_failed")
        trace.append(self._structured_trace("ata_mapping", mapping_response, combined_runtime_call=False))

        if mapping_response.error:
            return self._fallback(
                request,
                fields,
                identifiers,
                trace,
                [mapping_response.error, *warnings],
                facts=facts,
            )

        warnings.extend(mapping_warnings)
        if _has_schema_error(mapping_warnings):
            return self._fallback(request, fields, identifiers, trace, mapping_warnings, facts=facts)
        certificate_validation = validate_certificate(self.certificate, _mapping_atas(mapping))
        trace.append({"step": "certificate_scope_validation", "status": "completed", "entries": len(certificate_validation)})
        self._report(progress, "certificate_scope_validation", "ATA отдельно проверены по области сертификата.")

        if requested_mode == "auto" and self._needs_post_mapping_extended(mapping, facts):
            mode = "extended"
        critic_input = {
            "request": request,
            "engineering_facts": facts,
            "relations": facts.get("relations", []),
            "ata_mapping": mapping,
            "certificate_validation": certificate_validation,
            "critic_depth": mode,
        }
        critic_payload, critic_response = self._call_json(
            ATA_CRITIC_PROMPT,
            critic_input,
            "independent_critic",
            ATA_CRITIC_SCHEMA,
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
                ATA_CRITIC_SCHEMA,
                critic_warnings,
            )
            critic_actions, critic_warnings = validate_critic(critic_payload, mapping, facts)
        if _has_schema_error(critic_warnings) and critic_response.repair_attempted:
            critic_response = replace(critic_response, error="schema_validation_failed")
        trace.append(
            self._structured_trace(
                "independent_critic",
                critic_response,
                combined_runtime_call=False,
            )
        )
        if critic_response.error:
            warnings.append(critic_response.error)
        warnings.extend(critic_warnings)
        mapping, critic_actions, addition_warnings = apply_critic_additions(mapping, critic_actions, facts, request)
        warnings.extend(addition_warnings)
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
        required = self._required_inputs(facts, bool(assembled["affected_ata"]))
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
            certificate_validation,
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
            "certificate_validation": certificate_validation,
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
            "certificate_validation": certificate_validation,
            "certificate_catalog_available": bool(getattr(self.certificate, "entries", [])),
            **assembled,
            "required_input_data": ["engineering semantic classification by LLM or engineer"],
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
            return {}, replace(response, error="truncated_response")
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
            error = str(exc) or f"llm_stage_failed:{type(exc).__name__}"
            return self._repair_json(stage, system, payload, schema, [error])

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
            return {}, replace(response, error="truncated_response")
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
            return {}, replace(response, error=error)

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
            error=error,
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
            "structured_output_mode": response.structured_output_mode,
            "schema_enforced_by_server": response.schema_enforced,
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
    def _required_inputs(facts: dict[str, object], has_affected: bool) -> list[str]:
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
        certificate_validation: list[dict[str, object]],
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
        certificate_statuses = {
            str(item.get("certificate_scope_status") or "")
            for item in certificate_validation
            if isinstance(item, dict)
        }
        if not certificate_validation or "catalog_unavailable" in certificate_statuses:
            reasons.append("certificate_catalog_unavailable")
        if "ambiguous_subchapter" in certificate_statuses:
            reasons.append("certificate_scope_ambiguous")
        if "not_in_certificate" in certificate_statuses:
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
        certificate_scope = {
            "status": _certificate_summary(result["certificate_validation"]),
            "catalog_loaded": bool(result.get("certificate_catalog_available")),
            "matched": [
                {
                    "ata": item["ata"],
                    "certificate_ata": item["ata"],
                    "name": (item.get("certificate_entry") or {}).get("name", ""),
                }
                for item in result["certificate_validation"]
                if item.get("catalog_present")
            ],
            "unmatched": [item["ata"] for item in result["certificate_validation"] if not item.get("catalog_present")],
            "deprecated": True,
        }
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
                    "certificate": "scope_validation_only",
                },
                "compatibility": {"version": "v1", "deprecated_fields": True},
            }
        )

    @staticmethod
    def _answer(result: dict[str, object]) -> str:
        lines = ["## Предварительная оценка ATA", "", "Техническая классификация не является подтверждением capability."]
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
