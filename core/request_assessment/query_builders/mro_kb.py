from __future__ import annotations

from core.request_assessment.models import AssessmentState, SelectedSimilarCase


def build_case_query(case: SelectedSimilarCase, state: AssessmentState) -> str:
    aircraft = state.confirmed_inputs.aircraft_model or state.confirmed_inputs.aircraft_type or "не указан"
    ata = ", ".join([str(item) for item in (state.ata_impact or {}).get("affected_ata", [])]) or "не указаны"
    potential = ", ".join([str(item) for item in (state.ata_impact or {}).get("potentially_affected_ata", [])]) or "не указаны"
    deliverables = ", ".join(state.business_context.requested_deliverables) or "не указаны"
    return f"""По заявке {case.case_id} найди документы и подтверждающие разделы,
которые описывают:

- исходную техническую проблему;
- объект работ;
- тип повреждения;
- расположение;
- фактически выполненную инженерную работу;
- выпущенные repair drawing, repair instruction, damage assessment,
  stress substantiation или approved repair data;
- использованные ссылки на SRM, AMM и другие контролируемые документы;
- aircraft type, model, MSN и effectivity, если они явно указаны.

Контекст новой заявки:
- aircraft: {aircraft};
- работа: {_work_type(state.source.request_text)};
- объект: {_object(state)};
- повреждение: {_damage(state)};
- зона: {state.confirmed_inputs.zone or "не указана"};
- affected ATA: {ata};
- potentially affected ATA: {potential};
- требуемые результаты: {deliverables}.

Отдели прямое техническое совпадение от:
- простого упоминания ATA;
- совпадения только по местоположению;
- другого объекта;
- другого вида повреждения;
- другой инженерной задачи.

Не принимай решение о capability новой заявки.
Не делай выводов, которых нет в документах.

Верни:
- case_id;
- document_id;
- chunk_id;
- section_title;
- название и тип документа;
- подтверждающий фрагмент;
- aircraft, MSN и effectivity;
- объект и повреждение;
- фактически выпущенные документы;
- причины релевантности;
- ограничения применимости;
- противоречия и недостающие данные.
"""


def build_wide_query(state: AssessmentState) -> str:
    ata = ", ".join([str(item) for item in (state.ata_impact or {}).get("affected_ata", [])]) or "не указаны"
    potential = ", ".join([str(item) for item in (state.ata_impact or {}).get("potentially_affected_ata", [])]) or "не указаны"
    return f"""Выполни широкий документальный поиск для предварительной MRO assessment.

Цель поиска: найти контролируемые документы и завершенные MRO-кейсы, которые могут подтвердить технический scope.
Aircraft: {state.confirmed_inputs.aircraft_model or state.confirmed_inputs.aircraft_type or "не указан"}.
Work type: {_work_type(state.source.request_text)}.
Object: {_object(state)}.
Damage: {_damage(state)}.
Zone: {state.confirmed_inputs.zone or "не указана"}.
Direct ATA: {ata}.
Potential ATA: {potential}.
Requested deliverables: {", ".join(state.business_context.requested_deliverables) or "не указаны"}.
Required document types: repair drawing, repair instruction, damage assessment, stress substantiation, approved repair data.
Relevance criteria: direct object, damage, work type and applicability evidence.
Exclusion criteria: simple ATA mention, different object, different damage type, unrelated engineering task.

Верни structured evidence с document_id, chunk_id, section_title, snippet, relevance reasons, applicability limits and missing data.
"""


def _work_type(text: str) -> str:
    return "repair design" if "repair" in text.lower() or "ремонт" in text.lower() else "не указан"


def _object(state: AssessmentState) -> str:
    facts = ((state.ata_impact or {}).get("engineering_facts") or {})
    if isinstance(facts, dict):
        for key in ("object", "component", "damaged_object"):
            if facts.get(key):
                return str(facts[key])
    return "не указан"


def _damage(state: AssessmentState) -> str:
    facts = ((state.ata_impact or {}).get("engineering_facts") or {})
    if isinstance(facts, dict):
        for key in ("damage_type", "defect", "damage"):
            if facts.get(key):
                return str(facts[key])
    return "не указано"

