#!/usr/bin/env python3
"""Offline, leakage-safe ATA chapter benchmark for the first-pass agent."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.go_no_go import AtaImpactAgent


def chapter(code: str) -> str:
    normalized = code.upper().replace("ATA", "").strip()
    return normalized.split("-", 1)[0].strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="tests/fixtures/ata_impact_from_mro_rag.jsonl")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", type=Path, help="Write per-case predictions and aggregate metrics as JSON.")
    args = parser.parse_args()
    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = PROJECT_ROOT / fixture
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]
    # First-layer benchmark: documents and LLM are intentionally excluded so
    # historical work-order text cannot leak an answer into the classifier.
    os.environ["MRO_KB_ATA_AGENT_LLM_ENABLED"] = "0"
    agent = AtaImpactAgent()
    hits_at_1 = hits_at_3 = eligible = predicted_queries = exact_sets = 0
    true_positive = false_positive = false_negative = 0
    latencies_ms: list[float] = []
    cases: list[dict[str, object]] = []
    for row in rows:
        expected = {chapter(str(item)) for item in row.get("expected_ata") or []}
        if not expected:
            continue
        eligible += 1
        input_data = row.get("input") if isinstance(row.get("input"), dict) else row
        fields = {key: input_data[key] for key in ("aircraft_type", "msn", "case_type") if input_data.get(key) not in (None, "")}
        request = str(input_data.get("request") or input_data.get("description") or input_data.get("problem_summary") or "")
        started = time.perf_counter()
        result = agent.analyze(request, fields)
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies_ms.append(elapsed_ms)
        predicted = list(dict.fromkeys(chapter(str(item)) for item in result["direct_ata"]))
        predicted_set = set(predicted)
        intersection = expected & predicted_set
        hits_at_1 += bool(expected.intersection(predicted[:1]))
        hits_at_3 += bool(expected.intersection(predicted[:3]))
        predicted_queries += bool(predicted_set)
        exact_sets += predicted_set == expected
        true_positive += len(intersection)
        false_positive += len(predicted_set - expected)
        false_negative += len(expected - predicted_set)
        cases.append({
            "benchmark_id": row.get("benchmark_id"), "expected_chapters": sorted(expected),
            "predicted_chapters": predicted, "true_positive": sorted(intersection),
            "latency_ms": round(elapsed_ms, 3),
        })
    ordered_latencies = sorted(latencies_ms)
    percentile = lambda value: ordered_latencies[min(len(ordered_latencies) - 1, int((len(ordered_latencies) - 1) * value))] if ordered_latencies else 0.0
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    metrics = {
        "fixture": str(fixture), "eligible": eligible, "mode": "first_layer_no_llm_no_evidence",
        "chapter_hit_at_1": round(hits_at_1 / eligible, 4) if eligible else 0,
        "chapter_hit_at_3": round(hits_at_3 / eligible, 4) if eligible else 0,
        "chapter_precision_micro": round(precision, 4),
        "chapter_recall_micro": round(recall, 4),
        "chapter_f1_micro": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0,
        "exact_chapter_set_accuracy": round(exact_sets / eligible, 4) if eligible else 0,
        "queries_with_ata": predicted_queries,
        "queries_without_ata": eligible - predicted_queries,
        "latency_ms": {"mean": round(sum(latencies_ms) / len(latencies_ms), 3) if latencies_ms else 0, "p50": round(percentile(0.50), 3), "p95": round(percentile(0.95), 3)},
    }
    payload = {"metrics": metrics, "cases": cases}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
