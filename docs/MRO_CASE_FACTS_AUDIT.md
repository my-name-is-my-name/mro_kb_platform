# MRO case facts audit

Audit date: 2026-08-06. Baseline commit: `298a9fd260146d57ec665278e640319a5f736307`.

## Reproduction

```bash
python3 scripts/audit_mro_case_facts.py
python3 scripts/evaluate_mro_case_facts.py --disable-llm
```

The audit reads `data_runtime/mro_kb.sqlite3` in read-only mode. Qdrant is checked with an exact
`payload.case_id` filter and a filtered payload sample. When Qdrant is inaccessible, the point count is
`null` and the result contains `QDRANT_AUDIT_UNAVAILABLE` rather than claiming that the case has no points.

## Corpus results

| requested_case_id | resolved_case_id | method | case_found | documents | chunks | references | cited references | Qdrant points | warnings |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| `MP-0842` | - | `UNRESOLVED` | false | 13* | 2075* | 85* | 59* | 179* | diagnostic numeric candidate `MRO-842`; automatic linking forbidden |
| `MP-1147` | - | `UNRESOLVED` | false | 0 | 0 | 0 | 0 | 0 | none |
| `MP-0956` | - | `UNRESOLVED` | false | 0 | 0 | 0 | 0 | 0 | none |

`*` The corpus exists for `MRO-842`, but the requested `MP-0842` is not resolved to it. The only mapping
basis is the normalized numeric part, which is intentionally insufficient.

The required direct SQLite queries found `MRO-842` only. `MRO-1147` and `MRO-956` were absent from
`cases`, `documents`, and `chunks`. `document_references`, `chunk_references`, and
`case_document_links` contain rows for `MRO-842` only among the three candidate internal IDs.

## External ID provenance

All 267 current source JSON files contain `work_order_id`, which is used to construct the internal
`MRO-<number>` ID. None contains `source_case_id`,
`commercial_case_id`, `case_id`, `external_id`, or `aliases`, including nested alias metadata.
Consequently, the current source provides no verified `MP-*` or `WO-*` case aliases. Filenames and
document numbers such as `MP-842-ОПИ` are document identifiers and are not promoted to case aliases.
The service has no alias subsystem: only an exact existing `cases.case_id` resolves.

## Implementation discrepancies found

1. Human-facing `/api/chat` normalized any `MRO`, `MP`, or `WO` ID to an `MRO-*` ID by digits. That
   behavior remains unchanged for compatibility, but the machine-facing `/api/case-facts` does not use it.
2. There is no source basis for an alias registry, so none is introduced.
3. Vector search filtered `case_id` only after receiving global Qdrant results. Exact-case retrieval now
   sends a Qdrant filter and repeats the case check after retrieval.
4. Existing references were document-scoped in SQLite, but there was no fact-level usage/role contract.
5. The current configured LLM returned empty content during the 2026-08-06 extraction evaluation.
   The corpus remained `FOUND`; the response correctly used `FACT_EXTRACTION_UNAVAILABLE`.

## Golden evidence

The initial golden file contains one manually checked case, `MRO-842`. Its labels point to source chunks
that explicitly describe the FR35 crack, completed static-strength calculations, and the installed coating
system cited to SRM. No labels were added for 1147 or 956 because their source cases and documents are not
present.

## Evaluation snapshot

SQLite fallback evaluation (LLM disabled):

- ID resolution accuracy: `1.0` (one checked golden case)
- cross-case leakage count: `0`
- schema validation rate: `1.0`
- retrieval p50/p95: `1090.740 ms` / `1090.740 ms`
- facts returned: `0`; evidence coverage and grounded precision are therefore not applicable
- extraction latency: not applicable because LLM was disabled

Configured LLM evaluation with network access:

- ID resolution accuracy: `1.0`
- cross-case leakage count: `0`
- schema validation rate: `1.0`
- retrieval p50/p95: `1275.578 ms` / `1275.578 ms`
- extraction p50/p95: `27530.979 ms` / `27530.979 ms`
- facts returned: `0`, warning `FACT_EXTRACTION_UNAVAILABLE` (the LLM response content was empty)

These are single-case samples, not stable performance claims. The evaluation script intentionally has no
unit-test latency assertion.
