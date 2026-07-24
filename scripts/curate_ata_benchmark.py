#!/usr/bin/env python3
"""Conservatively curate ATA benchmark rows with an API-selected LLM reviewer."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime_clients import OpenAICompatibleLLM, RuntimeSettings


SYSTEM = """Ты — строгий эксперт по качеству тестовых данных MRO ATA.
Верни только JSON-массив объектов {"benchmark_id":"...","decision":"keep|drop","reason":"..."}.
Оставляй запись только если сам текст входящей заявки содержит конкретный технический объект/систему/дефект/работу и ожидаемая ATA выглядит правдоподобной. Удаляй шаблонные формулировки результата работ, общие запросы на документацию/одобрение, запросы без предмета и записи с очевидным конфликтом текста и ATA. Русский и английский одинаково допустимы. При сомнении выбирай drop. Не используй скрытые рассуждения и не добавляй записи."""

CURATION_EXCLUSIONS = {
    "mro_rag::009": "широкий список ATA без явной технической привязки к ним в запросе",
    "mro_rag::057": "описан результат работ, но нет объекта, дефекта или системы",
    "mro_rag::079": "не указана система/объект, к которому относится инструмент",
    "mro_rag::109": "общий reverse engineering без идентификации деталей и применимой ATA",
    "mro_rag::152": "недостаточно сведений об узле и области применения bolt-hinge",
    "mro_rag::240": "альтернативный материал указан без детали или зоны применения",
    "mro_rag::359": "одобрение процедур без технического объекта",
    "mro_rag::369": "смешаны Roller Track и две ATA без достаточной связи в кратком запросе",
    "mro_rag::382": "слишком общий запрос «ремонт коррозии» без объекта или зоны",
    "mro_rag::487": "требование по нагрузке на пол не обосновывает указанные ATA",
    "mro_rag::503": "замена кресел сопоставлена с чрезмерно широким списком ATA без ссылок",
    "mro_rag::505": "изменение MTOW сопоставлено с широким списком ATA без ссылок",
    "mro_rag::612": "выпуск ревизии КД без технического предмета",
}


def batches(values: list[dict[str, object]], size: int) -> list[list[dict[str, object]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def specificity_verdict(row: dict[str, object]) -> dict[str, str] | None:
    text = str((row.get("input") or {}).get("description") or "").lower()
    generic = ("технического решения", "документация по модификации", "выпуск ревизии", "технический отчет")
    anchors = re.compile(r"(?:корроз|трещ|поврежд|ремонт|шасс|крыл|фюзел|двер|шпангоут|стринг|закрыл|стойк|тормоз|гидравл|двигател|пилон|статик|приемник|обледен|кислород|\b(?:ata|amm|srm|sb|ad|p/?n|fr|rib|mlg|nlg)\b)", re.IGNORECASE)
    if any(term in text for term in generic) and not anchors.search(text):
        return {"decision": "drop", "reason": "запрос описывает выпуск документа/решения, но не содержит технический объект, дефект или систему"}
    if len(text) < 24:
        return {"decision": "drop", "reason": "слишком короткий запрос без достаточного технического контекста"}
    return None


def rule_review(rows: list[dict[str, object]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        identifier = str(row["benchmark_id"])
        if identifier in CURATION_EXCLUSIONS:
            result[identifier] = {"decision": "drop", "reason": CURATION_EXCLUSIONS[identifier]}
        else:
            result[identifier] = {"decision": "keep", "reason": "конкретный технический запрос; описание из реестра и ATA из связанной карточки прошли курацию"}
    return result


def review(llm: OpenAICompatibleLLM, rows: list[dict[str, object]], batch_size: int) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for batch_no, batch in enumerate(batches(rows, batch_size), start=1):
        forced = {str(row["benchmark_id"]): specificity_verdict(row) for row in batch}
        reviewable = [row for row in batch if forced[str(row["benchmark_id"])] is None]
        if not reviewable:
            result.update({key: value for key, value in forced.items() if value is not None})
            print(f"reviewed {min(batch_no * batch_size, len(rows))}/{len(rows)}", flush=True)
            continue
        prompt_rows = [
            {
                "benchmark_id": row["benchmark_id"],
                "request": row["input"]["description"],
                "expected_ata": row["expected_ata"],
            }
            for row in reviewable
        ]
        payload: list[object] = []
        for _ in range(2):
            raw = llm.chat(SYSTEM, json.dumps(prompt_rows, ensure_ascii=False), allow_reasoning_fallback=False)
            start, end = raw.find("["), raw.rfind("]")
            try:
                payload = json.loads(raw[start : end + 1]) if start >= 0 and end > start else []
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list) and len(payload) >= len(reviewable):
                break
        allowed = {str(row["benchmark_id"]) for row in reviewable}
        result.update({key: value for key, value in forced.items() if value is not None})
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict) or str(item.get("benchmark_id")) not in allowed:
                continue
            decision = str(item.get("decision") or "drop").lower()
            result[str(item["benchmark_id"])] = {"decision": "keep" if decision == "keep" else "drop", "reason": str(item.get("reason") or "нет достаточного основания для качественной ATA-метки")}
        for row in reviewable:
            result.setdefault(str(row["benchmark_id"]), {"decision": "drop", "reason": "LLM review не вернул валидное решение; исключено консервативно"})
        print(f"reviewed {min(batch_no * batch_size, len(rows))}/{len(rows)}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "tests/fixtures/ata_impact_from_mro_rag.jsonl")
    parser.add_argument("--kept", type=Path, default=PROJECT_ROOT / "tests/fixtures/ata_impact_from_mro_rag.jsonl")
    parser.add_argument("--rejected", type=Path, default=PROJECT_ROOT / "tests/fixtures/ata_impact_rejected_after_quality_review.jsonl")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--rule-only", action="store_true", help="Use checked transparent curation rules without an LLM call")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    llm = None if args.rule_only else OpenAICompatibleLLM(RuntimeSettings())
    decisions = rule_review(rows) if llm is None else review(llm, rows, max(1, args.batch_size))
    kept, rejected = [], []
    for row in rows:
        verdict = decisions[str(row["benchmark_id"])]
        row["quality_review"] = verdict
        (kept if verdict["decision"] == "keep" else rejected).append(row)
    args.kept.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + "\n", encoding="utf-8")
    args.rejected.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rejected) + "\n", encoding="utf-8")
    print(json.dumps({"kept": len(kept), "rejected": len(rejected), "mode": "rules" if llm is None else "llm", "model": llm.resolve_model() if llm is not None else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
