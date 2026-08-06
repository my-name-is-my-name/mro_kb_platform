#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import urllib.request
from pathlib import Path


DEFAULT_IDS = ["MP-0842", "MP-1147", "MP-0956"]


def normalized_digits(value: str) -> str:
    match = re.search(r"\d+", value or "")
    return (match.group(0).lstrip("0") or "0") if match else ""


def resolve_case_id(conn: sqlite3.Connection, requested: str) -> dict[str, object]:
    exact = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (requested,)).fetchall()
    exact_ids = sorted({str(row[0]) for row in exact})
    if len(exact_ids) == 1:
        return {
            "resolved_case_id": exact_ids[0],
            "resolution_method": "EXACT_INTERNAL_ID",
            "resolution_evidence": "exact match to cases.case_id",
            "candidate_case_ids": [],
        }

    return {
        "resolved_case_id": None,
        "resolution_method": "UNRESOLVED",
        "resolution_evidence": "requested ID does not exactly match cases.case_id",
        "candidate_case_ids": [],
    }


def qdrant_case_audit(url: str, collection: str, case_id: str) -> tuple[int | None, list[str]]:
    warnings: list[str] = []
    case_filter = {"must": [{"key": "case_id", "match": {"value": case_id}}]}
    try:
        count_request = urllib.request.Request(
            f"{url.rstrip('/')}/collections/{collection}/points/count",
            data=json.dumps({"filter": case_filter, "exact": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(count_request, timeout=5) as response:
            count_body = json.loads(response.read())
        point_count = int((count_body.get("result") or {}).get("count") or 0)

        scroll_request = urllib.request.Request(
            f"{url.rstrip('/')}/collections/{collection}/points/scroll",
            data=json.dumps({"filter": case_filter, "limit": min(256, max(1, point_count)), "with_payload": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(scroll_request, timeout=5) as response:
            scroll_body = json.loads(response.read())
        points = (scroll_body.get("result") or {}).get("points") or []
        if any(str((point.get("payload") or {}).get("case_id") or "") != case_id for point in points):
            warnings.append("QDRANT_CROSS_CASE_PAYLOAD_FOUND")
        return point_count, warnings
    except Exception as exc:
        return None, [f"QDRANT_AUDIT_UNAVAILABLE: {type(exc).__name__}"]


def audit(db_path: Path, requested_ids: list[str], qdrant_url: str, collection: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    results: list[dict[str, object]] = []
    try:
        for requested in requested_ids:
            resolution = resolve_case_id(conn, requested)
            resolved = resolution["resolved_case_id"]
            digits = normalized_digits(requested)
            diagnostic_candidates = []
            if not resolved and digits:
                diagnostic_candidates = sorted(
                    str(row[0])
                    for row in conn.execute("SELECT case_id FROM cases").fetchall()
                    if normalized_digits(str(row[0])) == digits
                )
            count_case_id = str(resolved or (diagnostic_candidates[0] if len(diagnostic_candidates) == 1 else ""))
            warnings: list[str] = []
            if not resolved and count_case_id:
                warnings.append("COUNTS_ARE_FOR_UNRESOLVED_NUMERIC_CANDIDATE")

            def count(table: str) -> int:
                if not count_case_id:
                    return 0
                return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE case_id = ?", (count_case_id,)).fetchone()[0])

            cited_reference_count = 0
            if count_case_id:
                cited_reference_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM (SELECT DISTINCT document_id, ref_id FROM chunk_references WHERE case_id = ?)",
                        (count_case_id,),
                    ).fetchone()[0]
                )
            qdrant_points, qdrant_warnings = (
                qdrant_case_audit(qdrant_url, collection, count_case_id) if count_case_id else (0, [])
            )
            warnings.extend(qdrant_warnings)
            results.append(
                {
                    "requested_case_id": requested,
                    **resolution,
                    "diagnostic_candidate_case_ids": diagnostic_candidates,
                    "case_found": bool(resolved),
                    "document_count": count("documents"),
                    "chunk_count": count("chunks"),
                    "reference_count": count("document_references"),
                    "cited_reference_count": cited_reference_count,
                    "qdrant_point_count": qdrant_points,
                    "warnings": warnings,
                }
            )
    finally:
        conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit exact-case MRO KB corpus coverage and ID resolution")
    parser.add_argument("case_ids", nargs="*", default=DEFAULT_IDS)
    parser.add_argument("--db", type=Path, default=Path("data_runtime/mro_kb.sqlite3"))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection", default="mro_kb_chunks")
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.case_ids, args.qdrant_url, args.collection), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
