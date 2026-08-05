from __future__ import annotations

from .models import ClarificationQuestion, MissingInformation


def build_questions(missing: list[MissingInformation]) -> list[ClarificationQuestion]:
    questions: list[ClarificationQuestion] = []
    for idx, item in enumerate(missing, start=1):
        if item.target != "CUSTOMER":
            continue
        question = _question_text(item)
        questions.append(
            ClarificationQuestion(
                question_id=f"Q-{idx:03d}",
                field=item.field,
                target=item.target,
                question=question,
                answer_type=_answer_type(item.field),
                required=item.importance.value == "BLOCKING",
                reason=item.reason,
                requested_attachments=[],
            )
        )
    return questions


def _question_text(item: MissingInformation) -> str:
    templates = {
        "aircraft.msn": "Укажите MSN воздушного судна.",
        "aircraft.registration": "Укажите регистрацию воздушного судна.",
        "aircraft.model": "Укажите точную модель воздушного судна.",
        "aircraft.aircraft_type": "Укажите тип воздушного судна.",
        "damage.type": "Укажите тип повреждения.",
        "damage.dimensions": "Укажите размеры повреждения и единицы измерения.",
        "damage.location": "Укажите точное расположение повреждения.",
        "zone": "Укажите зону или интервал расположения.",
        "location.zone": "Укажите зону или интервал расположения.",
        "part_number": "Укажите part number затронутого компонента.",
        "component.part_number": "Укажите part number затронутого компонента.",
        "requested_deliverables": "Укажите требуемые результаты работ.",
        "approval_expectation": "Укажите ожидаемый маршрут или формат одобрения.",
        "document_reference": "Укажите ссылку на документ или приложите документ.",
        "photos": "Приложите актуальные фотографии повреждения.",
        "drawings": "Приложите доступные чертежи или схемы.",
    }
    if item.field == "request.additional_data":
        return f"Для продолжения анализа предоставьте: {item.reason}"
    return templates.get(item.field, f"Уточните значение поля {item.field}: {item.reason}")


def _answer_type(field: str) -> str:
    if field.endswith("date"):
        return "date"
    if "dimension" in field:
        return "dimensions"
    if field in {"document_reference", "drawings"}:
        return "document_reference"
    if field in {"photos"}:
        return "file_upload"
    if field == "requested_deliverables":
        return "multiple_choice"
    if field == "approval_expectation":
        return "single_choice"
    return "text"
