from __future__ import annotations

from core.request_assessment.ata_context import normalize_ata_context
from core.request_assessment.models import AssessmentState, SelectedSimilarCase


def build_case_query(case: SelectedSimilarCase, state: AssessmentState) -> str:
    aircraft = state.confirmed_inputs.aircraft_model or state.confirmed_inputs.aircraft_type or "не указан"
    ctx = normalize_ata_context(state.ata_impact, _fields(state), state.business_context.requested_deliverables, state.source.request_text)
    ata = ", ".join(ctx.affected_ata) or "не указаны"
    potential = ", ".join(ctx.potentially_affected_ata) or "не указаны"
    deliverables = ", ".join(state.business_context.requested_deliverables) or "не указаны"
    return f"""По исторической заявке {case.case_id} найди подтверждённые документами сведения:

- что было исходной инженерной задачей;
- какой объект рассматривался;
- какое повреждение или событие анализировалось;
- какие инженерные работы выполнялись;
- какие анализы или расчёты проводились;
- какие документы выпускались;
- какие исходные данные запрашивались;
- какие дисциплины участвовали;
- какие технические references использовались;
- какие ограничения, риски или причины отказа были зафиксированы.

Для каждого факта укажи document_id, chunk_id и подтверждающий фрагмент.

Технический профиль новой заявки для объяснения релевантности и отличий:
- aircraft: {aircraft};
- работа: {ctx.work_type or "не указан"};
- объект: {_objects(ctx)};
- повреждение: {_damage(ctx)};
- зона: {", ".join(ctx.locations) or state.confirmed_inputs.zone or "не указана"};
- affected ATA: {ata};
- potentially affected ATA: {potential};
- требуемые результаты: {deliverables}.

Используй универсальные категории фактов:
- activities;
- analyses_or_calculations;
- documents;
- customer_inputs;
- disciplines;
- references;
- constraints;
- outcome.

Не принимай решение о capability новой заявки.
Не переноси автоматически исторический outcome на новую заявку.
Не придумывай отсутствующие сведения.
"""


def build_wide_query(state: AssessmentState) -> str:
    ctx = normalize_ata_context(state.ata_impact, _fields(state), state.business_context.requested_deliverables, state.source.request_text)
    ata = ", ".join(ctx.affected_ata) or "не указаны"
    potential = ", ".join(ctx.potentially_affected_ata) or "не указаны"
    return f"""Выполни широкий документальный поиск для предварительной MRO assessment.

Цель поиска: найти контролируемые документы и завершенные MRO-кейсы, которые могут подтвердить технический scope.
Aircraft: {state.confirmed_inputs.aircraft_model or state.confirmed_inputs.aircraft_type or "не указан"}.
Work type: {ctx.work_type or "UNKNOWN"}.
Object: {_objects(ctx)}.
Damage: {_damage(ctx)}.
Zone: {", ".join(ctx.locations) or state.confirmed_inputs.zone or "не указана"}.
Direct ATA: {ata}.
Potential ATA: {potential}.
Requested deliverables: {", ".join(state.business_context.requested_deliverables) or "не указаны"}.
Найди частичные исторические материалы по универсальным категориям: activities, analyses_or_calculations, documents, customer_inputs, disciplines, references, constraints, outcome.
Для каждого факта укажи document_id, chunk_id и подтверждающий фрагмент.
Не принимай решение о capability новой заявки и не придумывай отсутствующие сведения.
"""


def _fields(state: AssessmentState) -> dict[str, object]:
    fields = state.confirmed_inputs.model_dump(mode="json", exclude_none=True)
    fields.update(state.confirmed_additional_data)
    return fields


def _objects(ctx: object) -> str:
    values = list(getattr(ctx, "physical_objects", [])) + list(getattr(ctx, "structural_elements", []))
    return ", ".join(values) if values else "не указан"


def _damage(ctx: object) -> str:
    values = list(getattr(ctx, "damage_types", [])) + list(getattr(ctx, "damage_descriptions", []))
    return ", ".join(values) if values else "не указано"
