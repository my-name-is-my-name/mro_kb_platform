from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .ata_context import normalize_ata_context
from .models import (
    ApprovalRouteAssessment,
    AssessmentResultStatus,
    AssessmentState,
    CapabilityAssessment,
    CapabilityContext,
    CapabilityRegistryMode,
)


class CapabilityProvider(Protocol):
    @property
    def mode(self) -> CapabilityRegistryMode: ...
    @property
    def version(self) -> str | None: ...
    def precheck(self, context: CapabilityContext) -> CapabilityAssessment: ...
    def assess(self, context: CapabilityContext) -> CapabilityAssessment: ...
    def health(self) -> dict[str, object]: ...


class JsonCapabilityProvider:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.version: str | None = "unconfigured"
        self.mode = CapabilityRegistryMode.UNAVAILABLE
        self.verification_status = "UNVERIFIED"
        self.entries: list[dict[str, Any]] = []
        self.exclusions: list[dict[str, Any]] = []
        self.approval_routes: list[dict[str, Any]] = []
        self.load_warning: str | None = None
        if self.path:
            self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            self.load_warning = "registry_file_missing"
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("registry root must be object")
            self.version = str(payload.get("version") or "unconfigured")
            self.mode = CapabilityRegistryMode(str(payload.get("mode") or "UNAVAILABLE"))
            self.verification_status = str(payload.get("verification_status") or "UNVERIFIED")
            self.entries = [item for item in payload.get("capabilities", []) if isinstance(item, dict)] if isinstance(payload.get("capabilities", []), list) else []
            self.exclusions = [item for item in payload.get("exclusions", []) if isinstance(item, dict)] if isinstance(payload.get("exclusions", []), list) else []
            self.approval_routes = [item for item in payload.get("approval_routes", []) if isinstance(item, dict)] if isinstance(payload.get("approval_routes", []), list) else []
        except Exception as exc:
            self.mode = CapabilityRegistryMode.UNAVAILABLE
            self.entries = []
            self.load_warning = f"registry_malformed:{exc.__class__.__name__}"

    def precheck(self, context: CapabilityContext) -> CapabilityAssessment:
        return self.assess(context)

    def assess(self, context: CapabilityContext) -> CapabilityAssessment:
        base = {
            "registry_version": self.version,
            "registry_mode": self.mode,
            "verification_status": self.verification_status,
        }
        if self.mode == CapabilityRegistryMode.UNAVAILABLE:
            return CapabilityAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["Capability registry unavailable"], **base)
        if self.mode == CapabilityRegistryMode.CONTROLLED:
            exclusion = self._matched_exclusion(context)
            if exclusion:
                return CapabilityAssessment(
                    status=AssessmentResultStatus.FAIL,
                    hard_fail_reasons=[str(exclusion.get("reason") or "Controlled capability exclusion matched")],
                    dimension_results={"registry_mode": AssessmentResultStatus.PASS},
                    **base,
                )
        candidates = [entry for entry in self.entries if bool(entry.get("active", True))]
        evaluated = [_evaluate_entry(entry, context, self.mode) for entry in candidates]
        matches = [item for item in evaluated if item[0] == AssessmentResultStatus.PASS]
        if matches:
            entries = [item[1] for item in matches]
            routes = [_approval_route_from_entry(entry, str(route)) for entry in entries for route in (entry.get("approval_routes", []) if isinstance(entry.get("approval_routes"), list) else []) if str(route).strip()]
            if any(bool(entry.get("approval_routes_prohibited")) for entry in entries):
                routes.append(ApprovalRouteAssessment(route="NO_APPROVAL_ROUTE", status=AssessmentResultStatus.FAIL, limitations=["Controlled no-route rule matched"]))
            status = AssessmentResultStatus.PASS if self.mode == CapabilityRegistryMode.CONTROLLED else AssessmentResultStatus.REVIEW
            review_items = [] if status == AssessmentResultStatus.PASS else ["Capability registry is advisory; formal decision requires controlled source"]
            return CapabilityAssessment(
                status=status,
                matched_capability_ids=[str(entry.get("capability_id")) for entry in entries],
                review_items=review_items,
                source_documents=[str(entry.get("source_document")) for entry in entries if entry.get("source_document")],
                dimension_results=matches[0][2],
                matched_approval_routes=routes,
                **base,
            )
        if evaluated:
            best = evaluated[0]
            return CapabilityAssessment(
                status=AssessmentResultStatus.UNKNOWN if self.mode == CapabilityRegistryMode.CONTROLLED else AssessmentResultStatus.REVIEW,
                review_items=["No complete matching capability record"],
                dimension_results=best[2],
                **base,
            )
        return CapabilityAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["No matching capability record"], **base)

    def _matched_exclusion(self, context: CapabilityContext) -> dict[str, Any] | None:
        for rule in self.exclusions:
            if not bool(rule.get("active", True)):
                continue
            if str(rule.get("verification_status") or "") != "APPROVED":
                continue
            family = str(rule.get("aircraft_family") or "").upper()
            if family and context.aircraft_family and family != context.aircraft_family.upper():
                continue
            models = {str(item).upper() for item in rule.get("aircraft_models", [])} if isinstance(rule.get("aircraft_models"), list) else set()
            model = (context.aircraft_model or "").split("-")[0].upper()
            if models and model not in models:
                continue
            work = str(rule.get("work_type") or "").upper()
            if work and context.work_type and work != context.work_type.upper():
                continue
            return rule
        return None

    def health(self) -> dict[str, object]:
        status = "available" if self.mode == CapabilityRegistryMode.CONTROLLED and not self.load_warning else "unconfigured" if self.mode == CapabilityRegistryMode.UNAVAILABLE else "degraded"
        return {
            "status": status,
            "mode": self.mode.value,
            "version": self.version,
            "verification_status": self.verification_status,
            "warning": self.load_warning,
            "capabilities": len(self.entries),
        }


def build_capability_context(state: AssessmentState) -> CapabilityContext:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    fields.update(state.confirmed_additional_data)
    ata = normalize_ata_context(state.ata_impact, fields, state.business_context.requested_deliverables, state.source.request_text)
    deliverables = _normalize_deliverables(state.business_context.requested_deliverables)
    return CapabilityContext(
        aircraft_family=ata.aircraft_family,
        aircraft_model=ata.aircraft_model,
        product_scope=ata.aircraft_family,
        work_type=ata.work_type,
        affected_ata=ata.affected_ata,
        potentially_affected_ata=ata.potentially_affected_ata,
        disciplines=_required_disciplines(deliverables, state.source.request_text),
        deliverables=deliverables,
        approval_expectation=state.business_context.approval_expectation,
        jurisdiction=state.business_context.jurisdiction,
        physical_objects=ata.physical_objects + ata.structural_elements,
        damage_types=ata.damage_types,
        locations=ata.locations,
    )


def _evaluate_entry(entry: dict[str, Any], context: CapabilityContext, mode: CapabilityRegistryMode) -> tuple[AssessmentResultStatus, dict[str, Any], dict[str, AssessmentResultStatus]]:
    dimensions = {
        "product_scope": _pass_if(_family_ok(entry, context)),
        "aircraft_model_scope": _pass_if(_model_ok(entry, context)),
        "work_type_scope": _pass_if(bool(context.work_type) and str(entry.get("work_type") or "").upper() == context.work_type.upper()),
        "ata_scope": _ata_status(entry, context, mode),
        "discipline_scope": _covers_all(entry.get("disciplines"), context.disciplines),
        "deliverables_scope": _covers_all(entry.get("deliverables"), context.deliverables),
        "registry_mode": AssessmentResultStatus.PASS if mode == CapabilityRegistryMode.CONTROLLED else AssessmentResultStatus.REVIEW,
        "source_control": _source_status(entry, mode),
    }
    if all(value == AssessmentResultStatus.PASS for value in dimensions.values()):
        return AssessmentResultStatus.PASS, entry, dimensions
    if any(value == AssessmentResultStatus.FAIL for value in dimensions.values()):
        return AssessmentResultStatus.FAIL, entry, dimensions
    return AssessmentResultStatus.REVIEW, entry, dimensions


def _family_ok(entry: dict[str, Any], context: CapabilityContext) -> bool:
    return bool(context.aircraft_family) and str(entry.get("aircraft_family") or "").upper() == context.aircraft_family.upper()


def _model_ok(entry: dict[str, Any], context: CapabilityContext) -> bool:
    models = {str(item).upper() for item in entry.get("aircraft_models", [])} if isinstance(entry.get("aircraft_models"), list) else set()
    model = (context.aircraft_model or "").split("-")[0].upper()
    return bool(model) and (not models or model in models)


def _ata_status(entry: dict[str, Any], context: CapabilityContext, mode: CapabilityRegistryMode) -> AssessmentResultStatus:
    scope = {str(item).upper() for item in entry.get("ata_scope", [])} if isinstance(entry.get("ata_scope"), list) else set()
    affected = {str(item).upper() for item in context.affected_ata}
    potential = {str(item).upper() for item in context.potentially_affected_ata}
    if affected and not affected.issubset(scope):
        return AssessmentResultStatus.FAIL if mode == CapabilityRegistryMode.CONTROLLED else AssessmentResultStatus.REVIEW
    if potential and not potential.issubset(scope):
        return AssessmentResultStatus.REVIEW
    return AssessmentResultStatus.PASS


def _covers_all(value: Any, required: list[str]) -> AssessmentResultStatus:
    offered = {str(item).lower() for item in value} if isinstance(value, list) else set()
    required_set = {item.lower() for item in required}
    if not required_set:
        return AssessmentResultStatus.PASS
    return AssessmentResultStatus.PASS if required_set.issubset(offered) else AssessmentResultStatus.FAIL


def _source_status(entry: dict[str, Any], mode: CapabilityRegistryMode) -> AssessmentResultStatus:
    if mode != CapabilityRegistryMode.CONTROLLED:
        return AssessmentResultStatus.REVIEW
    required = ("capability_id", "source_document", "source_revision")
    if not all(str(entry.get(key) or "").strip() for key in required):
        return AssessmentResultStatus.REVIEW
    if str(entry.get("verification_status") or "") != "APPROVED":
        return AssessmentResultStatus.REVIEW
    return AssessmentResultStatus.PASS if bool(entry.get("active", True)) else AssessmentResultStatus.FAIL


def _approval_route_from_entry(entry: dict[str, Any], route: str) -> ApprovalRouteAssessment:
    return ApprovalRouteAssessment(
        route=route,
        status=AssessmentResultStatus.PASS,
        jurisdictions=[str(item) for item in entry.get("jurisdictions", [])] if isinstance(entry.get("jurisdictions"), list) else [],
        deliverables=[str(item) for item in entry.get("deliverables", [])] if isinstance(entry.get("deliverables"), list) else [],
        source_document=str(entry.get("source_document") or "") or None,
        source_revision=str(entry.get("source_revision") or "") or None,
        limitations=[str(item) for item in entry.get("limitations", [])] if isinstance(entry.get("limitations"), list) else [],
    )


def _pass_if(value: bool) -> AssessmentResultStatus:
    return AssessmentResultStatus.PASS if value else AssessmentResultStatus.FAIL


def _normalize_deliverables(values: list[str]) -> list[str]:
    mapping = {
        "damage assessment": "damage_assessment",
        "repair drawing": "repair_drawing",
        "repair instruction": "repair_instruction",
        "stress substantiation": "stress_substantiation",
        "approved repair data": "approved_repair_data",
        "compliance document": "compliance_document",
    }
    return [mapping.get(str(item).strip().lower(), str(item).strip().lower()) for item in values if str(item).strip()]


def _required_disciplines(deliverables: list[str], text: str) -> list[str]:
    result = {"structures"} if any("repair" in item or "damage" in item for item in deliverables) else set()
    if "stress_substantiation" in deliverables or "stress" in text.lower():
        result.add("stress")
    if {"repair_drawing", "repair_instruction"} & set(deliverables):
        result.add("design")
    if "approved_repair_data" in deliverables or "compliance_document" in deliverables:
        result.add("certification")
    for marker, discipline in (("avionic", "avionics"), ("electrical", "electrical"), ("system", "systems")):
        if marker in text.lower():
            result.add(discipline)
    return sorted(result)
