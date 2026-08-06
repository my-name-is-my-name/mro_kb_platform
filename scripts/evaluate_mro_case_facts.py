#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models.entities import CaseFactsRequest, CaseFactsResponse
from core.retrieval.case_facts import CaseFactsService
from storage.sqlite.store import SQLiteStore


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 3)


def fact_is_grounded(store: SQLiteStore, fact: dict[str, object], case_id: str) -> bool:
    chunk = store.fetch_chunk(str(fact.get("chunk_id") or ""))
    return bool(
        chunk
        and str(chunk.get("case_id") or "") == case_id
        and str(chunk.get("document_id") or "") == str(fact.get("document_id") or "")
        and str(fact.get("evidence_text") or "") in str(chunk.get("text") or "")
    )


def matches_expected_fact(fact: dict[str, object], expected: dict[str, object]) -> bool:
    return bool(
        fact.get("category") == expected.get("category")
        and str(expected.get("value_contains") or "").casefold() in str(fact.get("value") or "").casefold()
        and str(fact.get("chunk_id") or "") in set(expected.get("allowed_chunk_ids") or [])
    )


def evaluate(db_path: Path, golden_path: Path, disable_llm: bool) -> dict[str, object]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    if isinstance(golden, dict):
        golden = [golden]
    if not isinstance(golden, list):
        raise ValueError("golden file must contain an object or list of objects")

    store = SQLiteStore(db_path)
    service = CaseFactsService(store, llm=None) if disable_llm else CaseFactsService(store)
    retrieval_latencies: list[float] = []
    extraction_latencies: list[float] = []
    responses: list[tuple[dict[str, object], dict[str, object]]] = []

    for expected in golden:
        if not isinstance(expected, dict):
            continue
        request = CaseFactsRequest(case_id=str(expected.get("requested_case_id") or ""))
        resolution = store.resolve_case_id(request.case_id)
        corpus = store.fetch_case_corpus_summary(resolution.resolved_case_id) if resolution.resolved_case_id else None
        if not resolution.resolved_case_id or not corpus or corpus.document_count == 0 or corpus.chunk_count == 0:
            response = service.case_facts(request)
            responses.append((expected, response.model_dump(mode="json")))
            continue

        started = time.perf_counter()
        hits, retrieval_warnings, retrieval_available = service._retrieve_exact_case(
            resolution.resolved_case_id,
            request.categories,
            request.max_evidence_per_category,
        )
        retrieval_latencies.append((time.perf_counter() - started) * 1000)
        if not retrieval_available:
            response = service._response("RETRIEVAL_UNAVAILABLE", resolution, corpus, warnings=retrieval_warnings)
        elif service.llm is None:
            response = service._response(
                "FOUND",
                resolution,
                corpus,
                warnings=[*retrieval_warnings, "FACT_EXTRACTION_UNAVAILABLE"],
            )
        else:
            started = time.perf_counter()
            try:
                candidates = service._extract_candidates(resolution.resolved_case_id, request.categories, hits)
                facts, warnings = service._validate_facts(
                    candidates,
                    hits,
                    request.categories,
                    request.max_evidence_per_category,
                    include_references=True,
                )
                listed = service._listed_only_references(resolution.resolved_case_id, facts)
                if not facts:
                    warnings.append("NO_GROUNDED_FACTS")
                response = service._response(
                    "FOUND",
                    resolution,
                    corpus,
                    facts=facts,
                    listed_references=listed,
                    warnings=[*retrieval_warnings, *warnings],
                )
            except Exception:
                response = service._response(
                    "FOUND",
                    resolution,
                    corpus,
                    warnings=[*retrieval_warnings, "FACT_EXTRACTION_UNAVAILABLE"],
                )
            extraction_latencies.append((time.perf_counter() - started) * 1000)
        responses.append((expected, response.model_dump(mode="json")))

    id_correct = 0
    schema_valid = 0
    negative_total = 0
    negative_correct = 0
    returned_facts: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []
    expected_fact_total = 0
    expected_fact_matches = 0
    cross_case_leakage = 0
    evidence_covered = 0
    for expected, response in responses:
        try:
            CaseFactsResponse.model_validate(response)
            schema_valid += 1
        except Exception:
            pass
        if (
            response.get("resolution_method") == expected.get("expected_resolution")
            and response.get("resolved_case_id") == expected.get("expected_internal_case_id")
        ):
            id_correct += 1
        expected_status = str(expected.get("expected_status") or "")
        if expected_status and expected_status != "FOUND":
            negative_total += 1
            negative_correct += int(response.get("status") == expected_status)
        expected_facts = [item for item in expected.get("expected_facts", []) if isinstance(item, dict)]
        expected_fact_total += len(expected_facts)
        facts = [item for item in response.get("facts", []) if isinstance(item, dict)]
        for fact in facts:
            returned_facts.append((expected, response, fact))
            if fact.get("document_id") and fact.get("chunk_id") and fact.get("evidence_text"):
                evidence_covered += 1
            if str(fact.get("chunk_id") or "").startswith(str(response.get("resolved_case_id") or "") + "::") is False:
                cross_case_leakage += 1
        for expected_fact in expected_facts:
            if any(matches_expected_fact(fact, expected_fact) for fact in facts):
                expected_fact_matches += 1

    grounded_and_expected = 0
    for expected, response, fact in returned_facts:
        case_id = str(response.get("resolved_case_id") or "")
        expected_facts = [item for item in expected.get("expected_facts", []) if isinstance(item, dict)]
        if fact_is_grounded(store, fact, case_id) and any(matches_expected_fact(fact, item) for item in expected_facts):
            grounded_and_expected += 1

    total = len(responses)
    returned_count = len(returned_facts)
    return {
        "golden_cases": total,
        "facts_returned": returned_count,
        "id_resolution_accuracy": id_correct / total if total else None,
        "cross_case_leakage_count": cross_case_leakage,
        "evidence_coverage": evidence_covered / returned_count if returned_count else None,
        "schema_validation_rate": schema_valid / total if total else None,
        "grounded_precision": grounded_and_expected / returned_count if returned_count else None,
        "expected_fact_recall": expected_fact_matches / expected_fact_total if expected_fact_total else None,
        "negative_status_accuracy": negative_correct / negative_total if negative_total else None,
        "retrieval_latency_ms": {
            "samples": len(retrieval_latencies),
            "p50": round(statistics.median(retrieval_latencies), 3) if retrieval_latencies else None,
            "p95": percentile(retrieval_latencies, 0.95),
        },
        "fact_extraction_latency_ms": {
            "samples": len(extraction_latencies),
            "p50": round(statistics.median(extraction_latencies), 3) if extraction_latencies else None,
            "p95": percentile(extraction_latencies, 0.95),
        },
        "llm_disabled": disable_llm,
        "responses": [response for _, response in responses],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate evidence-backed MRO case facts")
    parser.add_argument("--db", type=Path, default=Path("data_runtime/mro_kb.sqlite3"))
    parser.add_argument("--golden", type=Path, default=Path("tests/fixtures/mro_kb/golden_case_facts.json"))
    parser.add_argument("--disable-llm", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.db, args.golden, args.disable_llm)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
