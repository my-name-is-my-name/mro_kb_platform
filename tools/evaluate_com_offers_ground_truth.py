from __future__ import annotations

import argparse
import json
import math
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.commercial_offers import CommercialOffersService, normalize_case_id, normalize_lookup
from core.config import WORKSPACE_ROOT


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"a": SPREADSHEET_NS, "r": REL_NS}


def _column_name(cell_ref: str) -> str:
    match = re.match(r"([A-Z]+)", cell_ref or "")
    return match.group(1) if match else ""


def read_xlsx(path: Path) -> dict[str, list[tuple[int, dict[str, str]]]]:
    """Read simple XLSX worksheets without external dependencies."""
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", NS):
                shared_strings.append("".join(text.text or "" for text in item.iter(f"{{{SPREADSHEET_NS}}}t")))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {rel.attrib["Id"]: rel.attrib["Target"].lstrip("/") for rel in relroot}
        sheets: dict[str, list[tuple[int, dict[str, str]]]] = {}
        sheet_nodes = workbook.find("a:sheets", NS)
        if sheet_nodes is None:
            return sheets
        for sheet in sheet_nodes:
            name = sheet.attrib["name"]
            relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
            target = relationships[relationship_id]
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            root = ET.fromstring(archive.read(target))
            rows: list[tuple[int, dict[str, str]]] = []
            for row in root.findall(".//a:sheetData/a:row", NS):
                values: dict[str, str] = {}
                for cell in row.findall("a:c", NS):
                    value_node = cell.find("a:v", NS)
                    value = "" if value_node is None else value_node.text or ""
                    if cell.attrib.get("t") == "s" and value:
                        value = shared_strings[int(value)]
                    values[_column_name(cell.attrib.get("r", ""))] = value.strip()
                if any(value.strip() for value in values.values()):
                    rows.append((int(row.attrib.get("r", "0")), values))
            sheets[name] = rows
        return sheets


def build_ground_truth(path: Path) -> list[dict[str, Any]]:
    sheets = read_xlsx(path)
    if "Поиск заявки" not in sheets or "Заявки" not in sheets:
        raise ValueError("Expected sheets 'Поиск заявки' and 'Заявки'")

    by_description: dict[str, set[str]] = defaultdict(set)
    for row_number, row in sheets["Заявки"]:
        if row_number == 1:
            continue
        case_id = normalize_case_id(row.get("K") or row.get("B") or "")
        if not case_id:
            continue
        for description in (row.get("N"), row.get("I")):
            if description:
                by_description[normalize_lookup(description)].add(case_id)

    ground_truth: list[dict[str, Any]] = []
    for row_number, row in sheets["Поиск заявки"]:
        query = (row.get("L") or "").strip()
        if row_number < 4 or not query:
            continue
        expected = sorted(by_description.get(normalize_lookup(query), set()))
        ground_truth.append({"row": row_number, "query": query, "expected": expected})
    return ground_truth


def _base_case_id(case_id: str) -> str:
    return re.sub(r"\.\d+$", "", case_id)


def expand_expected_ids(expected: set[str], registry_ids: set[str]) -> set[str]:
    expanded = set(expected)
    by_base: dict[str, set[str]] = defaultdict(set)
    for case_id in registry_ids:
        by_base[_base_case_id(case_id)].add(case_id)
    for case_id in list(expected):
        if case_id not in registry_ids:
            expanded.update(by_base.get(_base_case_id(case_id), set()))
    return expanded


def first_relevant_rank(ids: list[str], expected: set[str]) -> int | None:
    for index, case_id in enumerate(ids, start=1):
        if case_id in expected:
            return index
    return None


def ndcg_at(ids: list[str], expected: set[str], k: int) -> float:
    dcg = sum((1.0 if case_id in expected else 0.0) / math.log2(index + 2) for index, case_id in enumerate(ids[:k]))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(expected), k)))
    return dcg / ideal if ideal else 0.0


def _rank_ids(service: CommercialOffersService, query: str, limit: int, mode: str) -> list[str]:
    if mode == "hybrid":
        result = service.similar_cases(query, limit=limit)
        return [case["case_id"] for case in result.get("similar_cases", [])]
    if mode == "fallback":
        service._vectors = {}
        service._case_profile_vectors = {}
        result = service.similar_cases(query, limit=limit)
        return [case["case_id"] for case in result.get("similar_cases", [])]
    if mode == "lexical":
        items = service._lexical_candidate_cases(query, limit=limit)
        return [item["case"].get("case_id", "") for item in items]
    if mode == "semantic":
        items = service._semantic_candidate_cases(query, limit=limit)
        return [item["case"].get("case_id", "") for item in items]
    if mode == "profile":
        return [item["case_id"] for item in service.profile_semantic_cases(query, limit=limit)]
    if mode == "hybrid-profile":
        service.profile_search_enabled = True
        result = service.similar_cases(query, limit=limit)
        return [case["case_id"] for case in result.get("similar_cases", [])]
    raise ValueError(f"Unknown evaluation mode: {mode}")


def evaluate(ground_truth_path: Path, limit: int, mode: str, expand_base_ids: bool) -> dict[str, Any]:
    service = CommercialOffersService()
    service._llm = None
    registry_ids = {row.get("case_id", "") for row in service._registry if row.get("case_id")}
    rows = []
    started = time.time()
    for item in build_ground_truth(ground_truth_path):
        expected = set(item["expected"])
        if expand_base_ids:
            expected = expand_expected_ids(expected, registry_ids)
        if not expected:
            continue
        ids = _rank_ids(service, str(item["query"]), limit, mode)
        rank = first_relevant_rank(ids, expected)
        rows.append(
            {
                "row": item["row"],
                "query": item["query"],
                "expected": sorted(expected),
                "top": ids,
                "rank": rank,
            }
        )

    count = max(1, len(rows))
    metrics: dict[str, float] = {}
    for k in (1, 3, 5, 10):
        capped_k = min(k, limit)
        metrics[f"hit_at_{k}"] = sum(1 for row in rows if row["rank"] and row["rank"] <= capped_k) / count
    metrics["mrr"] = sum((1 / row["rank"]) if row["rank"] else 0.0 for row in rows) / count
    for k in (5, 10):
        capped_k = min(k, limit)
        metrics[f"precision_at_{k}"] = (
            sum(sum(1 for case_id in row["top"][:capped_k] if case_id in set(row["expected"])) / capped_k for row in rows) / count
        )
        metrics[f"recall_at_{k}"] = (
            sum(sum(1 for case_id in row["top"][:capped_k] if case_id in set(row["expected"])) / len(row["expected"]) for row in rows)
            / count
        )
        metrics[f"ndcg_at_{k}"] = sum(ndcg_at(row["top"], set(row["expected"]), capped_k) for row in rows) / count

    return {
        "ground_truth": str(ground_truth_path),
        "query_count": len(rows),
        "limit": limit,
        "mode": mode,
        "expand_base_case_ids": expand_base_ids,
        "seconds": round(time.time() - started, 2),
        "metrics": {key: round(value, 4) for key, value in metrics.items()},
        "misses_top10": [row for row in rows if not row["rank"] or row["rank"] > 10],
        "rank_gt5": [row for row in rows if row["rank"] and row["rank"] > 5],
        "rows": rows,
    }


def print_report(report: dict[str, Any]) -> None:
    print(json.dumps({key: value for key, value in report.items() if key not in {"rows"}}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate mro-similar-cases against com_offers/tests/ground truth.xlsx")
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=WORKSPACE_ROOT / "com_offers" / "tests" / "ground truth.xlsx",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--mode",
        choices=["hybrid", "fallback", "lexical", "semantic", "profile", "hybrid-profile"],
        default="hybrid",
        help="Retrieval mode to evaluate",
    )
    parser.add_argument("--disable-vectors", action="store_true", help="Deprecated alias for --mode fallback")
    parser.add_argument("--no-expand-base-case-ids", action="store_true", help="Do not expand MP-123 to MP-123.* variants")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    report = evaluate(
        ground_truth_path=args.ground_truth,
        limit=args.limit,
        mode="fallback" if args.disable_vectors else args.mode,
        expand_base_ids=not args.no_expand_base_case_ids,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(report)


if __name__ == "__main__":
    main()
