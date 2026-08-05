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
        "damage.dimensions": "Укажите размеры повреждения и единицы измерения.",
        "aircraft.registration": "Укажите регистрацию воздушного судна.",
        "request.additional_data": "Предоставьте недостающие исходные данные по заявке.",
    }
    return templates.get(item.field, f"Уточните значение поля {item.field}.")


def _answer_type(field: str) -> str:
    if field.endswith("date"):
        return "date"
    if "dimension" in field:
        return "dimensions"
    return "text"

