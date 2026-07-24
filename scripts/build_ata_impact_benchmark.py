"""Build a leakage-safe ATA benchmark from completed MRO_RAG work-order cards."""

from __future__ import annotations

import argparse
import csv
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

GENERIC_NON_TECHNICAL_ATA = {"00-00"}


def normalized_ata(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip().upper().replace("ATA ", "") for value in values if str(value).strip()} - GENERIC_NON_TECHNICAL_ATA)


def ground_truth_ids(path: Path) -> set[str]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as book:
        shared_root = ET.fromstring(book.read("xl/sharedStrings.xml"))
        shared = ["".join(node.text or "" for node in item.iterfind(".//m:t", ns)) for item in shared_root.findall("m:si", ns)]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet2.xml"))
    identifiers: set[str] = set()
    for cell in sheet.findall(".//m:c", ns):
        value = cell.findtext("m:v", default="", namespaces=ns)
        if cell.attrib.get("t") == "s" and value:
            value = shared[int(value)]
        match = re.fullmatch(r"MRO-(\d+)", str(value).strip(), re.IGNORECASE)
        if match:
            identifiers.add(match.group(1).zfill(3))
    return identifiers


def registry_descriptions(path: Path) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            match = re.search(r"(\d+)", str(row.get("case_id_raw") or ""))
            description = str(row.get("request_description") or "").strip()
            if match and description:
                descriptions[match.group(1).zfill(3)] = description
    return descriptions


def build(source: Path, destination: Path, registry: Path, ground_truth: Path) -> int:
    allowed_ids = ground_truth_ids(ground_truth)
    descriptions = registry_descriptions(registry)
    records: list[dict[str, object]] = []
    for path in sorted(source.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        work_order_id = str(card.get("work_order_id") or path.stem).zfill(3)
        description = descriptions.get(work_order_id, "")
        expected_ata = normalized_ata(card.get("ata_list"))
        if work_order_id not in allowed_ids or not description or not expected_ata:
            continue
        records.append(
            {
                "benchmark_id": f"mro_rag::{work_order_id}",
                "input": {
                    "description": description,
                    "aircraft_type": card.get("aircraft_type"),
                    "msn": card.get("msn"),
                    "case_type": card.get("case_type"),
                },
                "expected_ata": expected_ata,
                "expected_ata_chapters": sorted({value.split("-", 1)[0] for value in expected_ata}),
                "label_strength": "cross_source_quality_candidate",
                "direct_ata": [],
                "secondary_ata": [],
                "expert_review_status": "pending",
                "source": {
                    "system": "MRO_RAG",
                    "work_order_id": work_order_id,
                    "card_file": str(path),
                    "registry": str(registry),
                    "ground_truth": str(ground_truth),
                },
                "test_constraints": [
                    "Не передавать агенту documents или текст документов этого work order на первом проходе.",
                    "Заявка отобрана из com_offers/tests/ground truth.xlsx и описание взято из реестра заявок; ATA требует выборочной экспертной валидации.",
                ],
            }
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True, help="com_offers case_registry.csv")
    parser.add_argument("--ground-truth", type=Path, required=True, help="com_offers/tests/ground truth.xlsx")
    args = parser.parse_args()
    print(f"records={build(args.source, args.output, args.registry, args.ground_truth)}")


if __name__ == "__main__":
    main()
