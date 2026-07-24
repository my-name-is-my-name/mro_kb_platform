#!/usr/bin/env python3
"""Create a conservative, auditable ATA benchmark from cross-linked MRO data.

This script deliberately does not treat a work-order subject as a test query.
It uses the linked MRO-RAG problem summary only when it provides a concrete
description of the same work order.  A row is kept only when the selected text
names a defect/action *and* a real aircraft object/system, with a location,
part number, or other technical qualifier.  Regulatory or paperwork-only rows
are rejected even when they contain an ATA in their source card.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAG_ROOT = PROJECT_ROOT.parent / "MRO_RAG" / "apps" / "webapp" / "demo_data"

DEFECT_RE = re.compile(
    r"корроз|трещ|царап|пот[её]рт|поврежд|вмятин|забоин|отверсти|ослаблен|"
    r"несоответств|деформац|рассло|вздут|сквозн|износ|утеч|repair|corrosion|"
    r"crack|scratch|damage|dent|hole|wear|loosening|delaminat|discrepanc|abrasion",
    re.IGNORECASE,
)
OBJECT_RE = re.compile(
    r"крыл|обшивк|панел|нервюр|лонжерон|балк|шпангоут|стрингер|фитинг|фланц|"
    r"опор[аы].*шасс|стойк[аи]|шасс|цилиндр|тормоз|двер|порог|рельс|ролик|"
    r"закрыл|предкрыл|капот|створк|реверс|двигател|пилон|при[её]мник.*статик|"
    r"окн[ао]|рам[аеы]|бак|труб|изоляц|cowl|reverser|engine|wing|fuselage|"
    r"frame|stringer|spar|rib|fitting|gear|brake|flap|slat|static port|window|"
    r"panel|door|roller track|sliding tube|bifurcation",
    re.IGNORECASE,
)
QUALIFIER_RE = re.compile(
    r"\b(?:p/?n|s/?n|fr|sta|stgr|rib|stiff|mlg|nlg|lh|rh|y[+\-]?\d|z[+\-]?\d)\b|"
    r"шпангоут|стрингер|нервюр|крепеж|лев[а-я]*|прав[а-я]*|верхн[а-я]*|нижн[а-я]*|"
    r"между|район|зон[аеы]|кабин|багаж|центральн|топливн|fuselage|wing|left|right|"
    r"upper|lower|between|area|zone|cockpit|cargo",
    re.IGNORECASE,
)
PAPERWORK_RE = re.compile(
    r"документац\w*\s+разрабат|выпуска\s+технического\s+решени|"
    r"техническ\w*\s+отч[её]т\w*\s+о\s+подтверждени|"
    r"сервисн\w*\s+бюллетен\w*\s+разработан|"
    r"модификац\w*\s+по\s+запросу",
    re.IGNORECASE,
)
# These entries were read in full.  They describe an AD/AMOC task or have a
# truncated defect description, so using them as a direct ATA-identification
# query would teach the evaluator the wrong behaviour.
MANUAL_EXCLUSIONS = {
    "225": "описание обрывается до перечня зон коррозии",
    "447": "дубликат 460 с тем же описанием и конфликтующей дополнительной ATA 51",
    "633": "директива без зафиксированного дефекта на конкретном ВС",
    "861": "директива/модификация без конкретного дефекта на конкретном ВС",
    "875": "директива без зафиксированного дефекта на конкретном ВС",
    "895": "задача по AMOC и срокам модификации, без описания повреждения или объекта",
    "678": "дубликат заявки 612: то же описание коррозии композитной панели",
    "792": "описание обрывается до раскрытия характера механических повреждений",
}


def compact(value: object) -> str:
    text = " ".join(str(value or "").split())
    # The test query has no attached figures, so figure references are noise.
    text = re.sub(r"\s*\([^)]*(?:рисун|figure|fig\.)[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:см\.?|смотри|see)\s*(?:рисун|figure|fig\.)[^.]*\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*(?:зон[аы][^.]*|област[ьи][^.]*|мест[ао][^.]*|размер[аы][^.]*|"
        r"повреждени[ея][^.]*|детальн[а-я]*\s+информац[^.]*|результат[а-я]*\s+замер[^.]*)"
        r"(?:представлен[аыо]?|показан[аыо]?|указан[аыо]?)[^.]*"
        r"(?:рисун|figure|fig\.)[^.]*\.?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*(?:P/?N|S/?N|PN|SN)\s*[:#]?\s*[A-Z0-9][A-Z0-9./-]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*,", ",", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return " ".join(text.split()).strip()


def describe(text: str) -> dict[str, int | bool]:
    return {
        "has_defect": bool(DEFECT_RE.search(text)),
        "has_object": bool(OBJECT_RE.search(text)),
        "has_qualifier": bool(QUALIFIER_RE.search(text)),
        "is_paperwork_only": bool(PAPERWORK_RE.search(text)) and not bool(DEFECT_RE.search(text)),
        "length": len(text),
    }


def is_self_contained(signals: dict[str, int | bool]) -> bool:
    # A defect and a component are mandatory. A qualifier prevents vague
    # phrases such as "repair corrosion" from entering the benchmark.
    return bool(signals["has_defect"] and signals["has_object"] and signals["has_qualifier"] and not signals["is_paperwork_only"])


def specificity(signals: dict[str, int | bool]) -> int:
    return 3 * int(bool(signals["has_defect"])) + 3 * int(bool(signals["has_object"])) + 2 * int(bool(signals["has_qualifier"])) + min(int(signals["length"]) // 160, 2)


def select_description(registry_text: str, document_text: str) -> tuple[str, str, dict[str, int | bool]]:
    candidates = []
    for source, text in (("registry", registry_text), ("mro_rag_problem_summary", document_text)):
        signals = describe(text)
        candidates.append((is_self_contained(signals), specificity(signals), len(text), source, text, signals))
    accepted = [candidate for candidate in candidates if candidate[0]]
    choice = max(accepted or candidates, key=lambda candidate: (candidate[1], candidate[2]))
    _, _, _, source, text, signals = choice
    return source, text, signals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--kept", type=Path, required=True)
    parser.add_argument("--rejected", type=Path, required=True)
    parser.add_argument("--mro-rag-root", type=Path, default=DEFAULT_RAG_ROOT)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept, rejected = [], []
    seen_descriptions: dict[str, str] = {}
    for row in rows:
        work_order_id = str((row.get("source") or {}).get("work_order_id") or "").zfill(3)
        card_path = args.mro_rag_root / f"{work_order_id}.json"
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            card = {}
        registry_text = compact((row.get("input") or {}).get("description"))
        document_text = compact(card.get("problem_summary"))
        source, selected, signals = select_description(registry_text, document_text)
        rejection_reason = MANUAL_EXCLUSIONS.get(work_order_id)
        if not rejection_reason and not is_self_contained(signals):
            rejection_reason = "нет одновременно дефекта, конкретного объекта и локализующего технического признака"
        fingerprint = re.sub(r"[^a-zа-я0-9]+", "", selected.lower())
        if not rejection_reason and fingerprint in seen_descriptions:
            rejection_reason = f"дубликат описания заявки {seen_descriptions[fingerprint]}"
        decision = "drop" if rejection_reason else "keep"
        row["quality_review"] = {
            "decision": decision,
            "reason": rejection_reason or "самодостаточное описание: дефект, объект и техническая локализация подтверждены",
            "description_source": source,
            "signals": signals,
            "specificity_score": specificity(signals),
        }
        if decision == "keep":
            row["input"]["description"] = selected
            row["input"]["description_source"] = source
            row["test_constraints"] = [
                "Не передавать агенту документы или текст документов этого work order на первом проходе.",
                "Описание проверено по реестру и связанной карточке MRO-RAG; использован источник: " + source + ".",
                "expected_ata — техническая метка из связанной карточки, а не окончательное инженерное заключение; она требует выборочной экспертной валидации.",
            ]
            seen_descriptions[fingerprint] = work_order_id
            kept.append(row)
        else:
            rejected.append(row)
    args.kept.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + "\n", encoding="utf-8")
    args.rejected.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rejected) + "\n", encoding="utf-8")
    print(json.dumps({"kept": len(kept), "rejected": len(rejected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
