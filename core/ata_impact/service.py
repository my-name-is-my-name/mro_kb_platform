from __future__ import annotations

import json
from typing import Callable, Protocol

from .certificate_validator import validate_certificate
from .evidence import AtaEvidenceRetriever, NullAtaEvidenceRetriever
from .identifiers import extract_identifiers
from .models import MAPPING_CATEGORIES, empty_mapping
from .prompts import (
    ATA_CRITIC_PROMPT,
    ATA_MAPPING_AND_CRITIC_PROMPT,
    ATA_MAPPING_PROMPT,
    ENGINEERING_FACT_EXTRACTION_PROMPT,
)
from .validator import apply_critic_additions, assemble, validate_critic, validate_facts, validate_mapping


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
        fields = fields if isinstance(fields, dict) else {}
        identifiers = extract_identifiers(request, fields)
        trace: list[dict[str, object]] = [
            {"step": "identifier_extraction", "status": "completed", "identifiers": identifiers}
        ]
        warnings: list[str] = []
        self._report(progress, "identifier_extraction", "Формальные идентификаторы извлечены.")

        if self.llm is None:
            return self._fallback(request, fields, identifiers, trace)

        facts_payload, fact_error = self._call_json(
            ENGINEERING_FACT_EXTRACTION_PROMPT,
            {"request": request, "fields": fields, "identifiers": identifiers},
        )
        facts, fact_warnings = validate_facts(facts_payload, identifiers)
        warnings.extend(fact_warnings)
        trace.append(
            {
                "step": "engineering_fact_extraction",
                "status": "error" if fact_error else "completed",
                "warning": fact_error,
            }
        )
        if fact_error:
            return self._fallback(request, fields, identifiers, trace, [fact_error, *warnings])
        if _has_schema_error(fact_warnings):
            return self._fallback(request, fields, identifiers, trace, fact_warnings, facts=facts)
        self._report(progress, "engineering_fact_extraction", "Инженерная модель заявки сформирована.")

        mode = self._select_mode(runtime_mode, identifiers, facts)
        catalog = self._catalog_payload()
        mapping_input = {
            "request": request,
            "engineering_facts": facts,
            "relations": facts.get("relations", []),
            "certificate_catalog": catalog,
            "explicit_user_ata": identifiers["explicit_ata"],
        }
        if mode == "standard":
            combined, mapping_error = self._call_json(ATA_MAPPING_AND_CRITIC_PROMPT, mapping_input)
            mapping_payload = combined.get("ata_mapping", combined) if isinstance(combined, dict) else {}
            critic_payload = combined.get("critic", {}) if isinstance(combined, dict) else {}
            trace.append({"step": "ata_mapping", "status": "error" if mapping_error else "completed", "combined_runtime_call": True})
            trace.append({"step": "independent_critic", "status": "error" if mapping_error else "completed", "combined_runtime_call": True})
        else:
            mapping_payload, mapping_error = self._call_json(ATA_MAPPING_PROMPT, mapping_input)
            critic_payload = {}
            trace.append({"step": "ata_mapping", "status": "error" if mapping_error else "completed", "combined_runtime_call": False})

        if mapping_error:
            trace[-1]["warning"] = mapping_error
            return self._fallback(request, fields, identifiers, trace, [mapping_error, *warnings], facts=facts)

        mapping, mapping_warnings = validate_mapping(mapping_payload, facts, list(identifiers["explicit_ata"]), request)
        warnings.extend(mapping_warnings)
        if _has_schema_error(mapping_warnings):
            return self._fallback(request, fields, identifiers, trace, mapping_warnings, facts=facts)
        certificate_validation = validate_certificate(self.certificate, _mapping_atas(mapping))
        trace.append({"step": "certificate_scope_validation", "status": "completed", "entries": len(certificate_validation)})
        self._report(progress, "certificate_scope_validation", "ATA отдельно проверены по области сертификата.")

        if mode == "extended":
            critic_payload, critic_error = self._call_json(
                ATA_CRITIC_PROMPT,
                {
                    **mapping_input,
                    "ata_mapping": mapping,
                    "certificate_validation": certificate_validation,
                },
            )
            trace.append({"step": "independent_critic", "status": "error" if critic_error else "completed", "combined_runtime_call": False})
            if critic_error:
                return self._fallback(request, fields, identifiers, trace, [critic_error, *warnings], facts=facts)
        elif runtime_mode == "auto" and self._needs_post_mapping_extended(mapping, facts):
            mode = "extended"
            critic_payload, critic_error = self._call_json(
                ATA_CRITIC_PROMPT,
                {**mapping_input, "ata_mapping": mapping, "certificate_validation": certificate_validation},
            )
            trace.append({"step": "independent_critic", "status": "error" if critic_error else "completed", "combined_runtime_call": False, "escalated_after_mapping": True})
            if critic_error:
                return self._fallback(request, fields, identifiers, trace, [critic_error, *warnings], facts=facts)
        critic_actions, critic_warnings = validate_critic(critic_payload, mapping, facts)
        warnings.extend(critic_warnings)
        if _has_schema_error(critic_warnings):
            return self._fallback(request, fields, identifiers, trace, critic_warnings, facts=facts)
        mapping, critic_actions, addition_warnings = apply_critic_additions(mapping, critic_actions, facts, request)
        warnings.extend(addition_warnings)
        # Critic additions are validated and then receive the same deterministic
        # certificate/document processing as mapper candidates.
        certificate_validation = validate_certificate(self.certificate, _mapping_atas(mapping))
        self._report(progress, "independent_critic", "Критик проверил роли и основания ATA.")

        document_types = ["AMM", "SRM", "IPC", "CMM", "WDM", "NTM", "ALS", "approved_repair_data"]
        try:
            evidence = self.evidence_retriever.search(
                request=request,
                engineering_facts=facts,
                ata_candidates=_mapping_atas(mapping),
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
        decision = (
            "completed"
            if assembled["affected_ata"] and not assembled["potentially_affected_ata"] and evidence["status"] == "completed"
            else ("completed_with_hypotheses" if assembled["affected_ata"] or assembled["potentially_affected_ata"] or assembled["context_ata"] else "engineering_review_required")
        )
        result = {
            "agent": "ata_impact",
            "contract_version": "v2",
            "runtime_mode": mode,
            "input": {"request": request, "aircraft_type": fields.get("aircraft_type"), "fields": fields},
            "identifiers": identifiers,
            "engineering_facts": facts,
            "ata_mapping": mapping,
            "certificate_validation": certificate_validation,
            "certificate_catalog_available": bool(getattr(self.certificate, "entries", [])),
            **assembled,
            "required_input_data": required,
            "document_verification": evidence,
            "decision": decision,
            "needs_human_approval": True,
            "agent_trace": trace,
            "warnings": list(dict.fromkeys([*warnings, *evidence["warnings"]])),
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
                "ata": ata,
                "confidence": 1.0,
                "status": "unverified",
                "reason": "Explicitly declared ATA; semantic LLM analysis unavailable",
                "source_fragment": ata,
            }
            for ata in identifiers["explicit_ata"]
        ]
        if facts is None:
            facts, fact_warnings = validate_facts({}, identifiers)
        else:
            fact_warnings = []
        certificate_validation = validate_certificate(self.certificate, list(identifiers["explicit_ata"]))
        evidence = NullAtaEvidenceRetriever().search(request, facts, list(identifiers["explicit_ata"]), facts["aircraft"], [], 0).as_dict()  # type: ignore[arg-type]
        assembled = assemble(mapping, [], certificate_validation, evidence)
        lexical_candidates = self._lexical_candidates(request)
        trace.append(
            {
                "step": "fallback",
                "status": "engineering_review_required",
                "mechanism": "explicit_ata_only",
                "lexical_candidates": lexical_candidates,
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
            "certificate_validation": certificate_validation,
            "certificate_catalog_available": bool(getattr(self.certificate, "entries", [])),
            **assembled,
            "required_input_data": ["engineering semantic classification by LLM or engineer"],
            "document_verification": evidence,
            "decision": "engineering_review_required",
            "needs_human_approval": True,
            "agent_trace": trace,
            "warnings": list(dict.fromkeys([*(warnings or []), *fact_warnings, "llm_unavailable_or_invalid", *evidence["warnings"]])),
            "lexical_ata_candidates": lexical_candidates,
            "capability_screening": "not_assessed",
        }
        self._add_compatibility(result, identifiers)
        result["answer"] = self._answer(result)
        return result

    def _call_json(self, system: str, payload: dict[str, object]) -> tuple[dict[str, object], str | None]:
        if self.llm is None:
            return {}, "llm_unavailable"
        try:
            # Some OpenAI-compatible reasoning models place their structured
            # answer in reasoning_content. We parse and retain only the JSON
            # object; raw reasoning is never stored or exposed in trace.
            raw = self.llm.chat(system, json.dumps(payload, ensure_ascii=False), allow_reasoning_fallback=True)
            stripped = raw.strip()
            if stripped.startswith("```"):
                stripped = stripped.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                candidates = _embedded_json_objects(stripped)
                if not candidates:
                    raise
                value = max(candidates, key=lambda item: _structured_output_score(item, system))
            if not isinstance(value, dict):
                return {}, "llm_response_not_object"
            return value, None
        except Exception as exc:
            return {}, f"llm_stage_failed:{type(exc).__name__}"

    def _catalog_payload(self) -> list[dict[str, object]]:
        entries = getattr(self.certificate, "entries", [])
        return [
            {
                "ata": str(getattr(entry, "ata", "")),
                "name": str(getattr(entry, "name", "")),
                "description": str(getattr(entry, "explanation", "")),
            }
            for entry in entries
        ]

    def _lexical_candidates(self, request: str) -> list[dict[str, object]]:
        matcher = getattr(self.certificate, "description_matches", None)
        if not callable(matcher):
            return []
        return [
            {"ata": ata, "status": "candidate_only", "matched_terms": terms}
            for ata, terms in matcher(request).items()
        ]

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
        conflict = any(item.get("status") == "conflicting" for item in mapping["user_declared_ata"])
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
            {**item, "status": "hypothesis", "deprecated": True}
            for status in ("possible_interface", "possible_procedure")
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
                "controlled_evidence": [
                    {
                        key: item.get(key)
                        for key in ("document_id", "title", "document_type", "revision", "effectivity", "section_reference", "trust_level")
                        if item.get(key) not in (None, "")
                    }
                    for item in result["document_verification"]["documents"]
                    if isinstance(item, dict)
                    and str(item.get("trust_level") or "").lower() in {"controlled_oem", "approved_data"}
                ],
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
                "schema_missing_required:",
                "schema_item_missing_required:",
                "schema_type_error:",
                "duplicate_entity_id",
                "duplicate_relation_id",
            )
        )
        for warning in warnings
    )


def _embedded_json_objects(text: str) -> list[dict[str, object]]:
    """Extract complete JSON objects without retaining surrounding reasoning."""
    result: list[dict[str, object]] = []
    for start, character in enumerate(text):
        if character != "{":
            continue
        depth = 0
        quoted = False
        escaped = False
        for end in range(start, len(text)):
            current = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    quoted = False
                continue
            if current == '"':
                quoted = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : end + 1])
                        if isinstance(value, dict):
                            result.append(value)
                    except json.JSONDecodeError:
                        pass
                    break
    return result


def _structured_output_score(value: dict[str, object], system: str) -> int:
    if system == ENGINEERING_FACT_EXTRACTION_PROMPT:
        expected = {"aircraft", "event", "physical_objects", "functional_purposes", "locations", "structural_elements", "damage", "relations", "uncertainties"}
    elif system == ATA_CRITIC_PROMPT:
        expected = {"actions"}
    else:
        expected = {"ata_mapping", "critic"} if system == ATA_MAPPING_AND_CRITIC_PROMPT else set(MAPPING_CATEGORIES)
    return len(expected.intersection(value))
