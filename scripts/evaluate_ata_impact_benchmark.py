#!/usr/bin/env python3
"""ATA chapter benchmark with explicit legacy, fallback and real-LLM modes."""
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
    parser.add_argument(
        "--mode",
        choices=("legacy-rules", "v2-fallback", "v2-llm"),
        default="legacy-rules",
        help="legacy-rules preserves the deprecated offline baseline; v2-llm measures the new semantic pipeline.",
    )
    args = parser.parse_args()
    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = PROJECT_ROOT / fixture
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]
    if args.mode in {"legacy-rules", "v2-fallback"}:
        os.environ["MRO_KB_ATA_AGENT_LLM_ENABLED"] = "0"
    elif os.getenv("MRO_KB_ATA_AGENT_LLM_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("v2-llm requires MRO_KB_ATA_AGENT_LLM_ENABLED=1")
    agent = AtaImpactAgent()
    if args.mode == "v2-llm":
        health = agent.health()
        if not health.get("llm_critic", {}).get("enabled"):
            raise SystemExit("v2-llm requested but ATA LLM is disabled")
        llm_health = agent._llm.health() if agent._llm is not None else {"ok": False}
        if not llm_health.get("ok"):
            raise SystemExit(f"v2-llm endpoint is unavailable: {llm_health.get('error', 'unknown error')}")
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
        if args.mode == "legacy-rules" and os.getenv(
            "MRO_KB_ENABLE_LEGACY_ATA_MODES", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            raise SystemExit(
                "legacy-rules requires MRO_KB_ENABLE_LEGACY_ATA_MODES=true"
            )
        runtime_mode = "rules_only" if args.mode == "legacy-rules" else "auto"
        result = agent.analyze(request, fields, mode=runtime_mode)
        if args.mode == "v2-llm" and result.get("runtime_mode") == "fallback":
            raise RuntimeError(f"v2-llm fell back for benchmark case {row.get('benchmark_id')}: {result.get('warnings')}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies_ms.append(elapsed_ms)
        source = result["direct_ata"] if args.mode == "legacy-rules" else result["affected_ata"]
        predicted = list(dict.fromkeys(chapter(str(item)) for item in source))
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
        "fixture": str(fixture), "eligible": eligible, "mode": args.mode,
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
