from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .models import AssessmentResultStatus, CapabilityAssessment, CapabilityContext


class CapabilityProvider(Protocol):
    def precheck(self, context: CapabilityContext) -> CapabilityAssessment: ...
    def assess(self, context: CapabilityContext) -> CapabilityAssessment: ...


class JsonCapabilityProvider:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.version = None
        self.entries: list[dict[str, object]] = []
        if self.path and self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.version = str(payload.get("version") or "")
            self.entries = [item for item in payload.get("capabilities", []) if isinstance(item, dict)]

    def precheck(self, context: CapabilityContext) -> CapabilityAssessment:
        return self.assess(context)

    def assess(self, context: CapabilityContext) -> CapabilityAssessment:
        if self.path and not self.path.exists():
            return CapabilityAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["Capability registry unavailable"])
        if not self.entries:
            return CapabilityAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["Capability registry unavailable"], registry_version=self.version)
        matches = [entry for entry in self.entries if _matches(entry, context)]
        active_matches = [entry for entry in matches if bool(entry.get("active", True))]
        if active_matches:
            review_items = []
            limitations = []
            for entry in active_matches:
                limitations.extend([str(item) for item in entry.get("limitations", []) if str(item).strip()] if isinstance(entry.get("limitations"), list) else [])
            status = AssessmentResultStatus.REVIEW if limitations else AssessmentResultStatus.PASS
            return CapabilityAssessment(
                status=status,
                matched_capability_ids=[str(entry.get("capability_id") or "") for entry in active_matches],
                review_items=review_items,
                limitations=limitations,
                source_documents=[str(entry.get("source_document") or "") for entry in active_matches if entry.get("source_document")],
                registry_version=self.version,
            )
        hard_fail = _find_hard_fail_reason(self.entries, context)
        if hard_fail:
            return CapabilityAssessment(status=AssessmentResultStatus.FAIL, hard_fail_reasons=[hard_fail], registry_version=self.version)
        return CapabilityAssessment(status=AssessmentResultStatus.UNKNOWN, review_items=["No matching capability record"], registry_version=self.version)


def build_capability_context(state: object) -> CapabilityContext:
    confirmed = getattr(state, "confirmed_inputs")
    ata_impact = getattr(state, "ata_impact") or {}
    business = getattr(state, "business_context")
    aircraft = confirmed.aircraft_type or confirmed.aircraft_model
    return CapabilityContext(
        aircraft_family=_family(aircraft),
        aircraft_model=confirmed.aircraft_model or confirmed.aircraft_type,
        work_type=_infer_work_type(getattr(getattr(state, "source"), "request_text", "")),
        ata=[str(item) for item in (ata_impact.get("affected_ata") or [])],
        disciplines=_infer_disciplines(getattr(getattr(state, "source"), "request_text", "")),
        deliverables=list(business.requested_deliverables or []),
        approval_expectation=business.approval_expectation,
        jurisdiction=business.jurisdiction,
    )


def _matches(entry: dict[str, object], context: CapabilityContext) -> bool:
    family_ok = not context.aircraft_family or str(entry.get("aircraft_family") or "").upper() == context.aircraft_family.upper()
    models = [str(item).upper() for item in entry.get("aircraft_models", [])] if isinstance(entry.get("aircraft_models"), list) else []
    model_root = (context.aircraft_model or "").split("-")[0].upper()
    model_ok = not model_root or not models or model_root in models
    work_ok = not context.work_type or str(entry.get("work_type") or "").upper() == context.work_type.upper()
    scope = {str(item).upper() for item in entry.get("ata_scope", [])} if isinstance(entry.get("ata_scope"), list) else set()
    ata_ok = not context.ata or not scope or any(str(item).upper() in scope for item in context.ata)
    deliverables = {str(item).lower() for item in entry.get("deliverables", [])} if isinstance(entry.get("deliverables"), list) else set()
    deliv_ok = not context.deliverables or all(item.lower() in deliverables for item in context.deliverables)
    return family_ok and model_ok and work_ok and ata_ok and deliv_ok


def _find_hard_fail_reason(entries: list[dict[str, object]], context: CapabilityContext) -> str | None:
    families = {str(entry.get("aircraft_family") or "").upper() for entry in entries}
    if context.aircraft_family and context.aircraft_family.upper() not in families:
        return f"Aircraft family {context.aircraft_family} is outside capability scope"
    return None


def _family(value: str | None) -> str | None:
    text = (value or "").upper()
    if "A320" in text or "A319" in text or "A321" in text:
        return "A320"
    if text.startswith("B737"):
        return "B737"
    return text.split("-")[0] if text else None


def _infer_work_type(text: str) -> str | None:
    lower = text.lower()
    if "repair" in lower or "ремонт" in lower:
        return "REPAIR_DESIGN"
    return None


def _infer_disciplines(text: str) -> list[str]:
    lower = text.lower()
    result = []
    if "stress" in lower or "прочност" in lower:
        result.append("stress")
    if "repair" in lower or "drawing" in lower:
        result.append("design")
    return result

