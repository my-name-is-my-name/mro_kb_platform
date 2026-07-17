# LLM Case Profile Experiment

## Goal

Test whether Qwen-generated structured case profiles improve `mro-similar-cases` quality over the current hybrid pipeline.

The experiment must be data-driven:

- no query-specific mappings;
- no benchmark answers in runtime;
- no hand-written synonym or case-form dictionaries;
- no aircraft type as a retrieval factor.

## Current Pipeline

Current retrieval compares a user query with case-card text assembled from:

- `case_registry.csv`;
- `Reestr_zayavok.xlsm`;
- converted Markdown manifest;
- exact engineering identifiers.

Signals:

- semantic embeddings over case-card text;
- lexical/BM25-like scoring;
- exact identifier matching;
- optional GPU reranker.

Strengths:

- transparent;
- fast after embeddings are built;
- robust for exact terms like `RIB 5`, `FR35.3`, `AD`, `P/N`, `MSN`;
- easy to audit.

Weaknesses:

- short Russian queries often do not overlap enough with long English/OCR descriptions;
- raw converted Markdown can be too verbose for embeddings;
- semantically important distinctions can be buried in procedure/drawing text.

## Profile Pipeline Hypothesis

Instead of embedding raw case-card text, build compact profiles in a shared schema.

Offline case profile:

```json
{
  "problem_summary": "",
  "work_type": "",
  "defect_type": "",
  "aircraft_type_metadata": "",
  "components": [],
  "zones": [],
  "identifiers": [],
  "action_required": "",
  "constraints_or_risks": [],
  "search_terms_ru_en": [],
  "evidence_fields": []
}
```

Online query profile uses the same schema, but must not produce or request target case IDs.

Expected benefit:

- compare defect/component/zone/action fields directly;
- reduce OCR noise before embedding;
- make Russian short queries and English source text closer in representation.

`aircraft_type_metadata` is retained in `metadata_text` for display/check/tie-break analysis. It is deliberately separated from the main profile `search_text`, so it is available to the pipeline but cannot dominate the primary semantic similarity.

Expected risk:

- LLM may omit rare identifiers;
- LLM may normalize away important exact details;
- profile generation can be slow and must run offline/background;
- incorrect profiles can make ranking worse while looking plausible.

## Architecture

Planned artifacts:

- `data_runtime/com_offers_case_profiles.jsonl`
- optional `data_runtime/com_offers_case_profile_vectors.json`
- profile progress/status JSON, equivalent to reindex progress.

Planned commands:

- `build-com-offer-profiles`: generate or refresh profiles in resumable mode;
- `com-offer-profiles-status`: inspect progress/cache status;
- `reindex-com-offer-profile-vectors`: embed profile search text;
- `com-offer-profile-vectors-status`: inspect profile vector progress/cache status;
- evaluation flag to compare profile-only and hybrid+profile modes.

Runtime behavior:

1. If profile index is missing/stale, the service continues with current hybrid retrieval.
2. `/api/health` reports profile status and warnings.
3. Profile retrieval is not enabled by default until metrics justify it.
4. Query profile generation is optional and must be controlled by an explicit environment flag.
5. Exact and lexical retrieval remain active as safety net.

Implemented command surface:

```bash
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles --limit 5
python3 -m apps.api.server com-offer-profiles-status
python3 -m apps.api.server reindex-com-offer-profile-vectors
python3 -m apps.api.server com-offer-profile-vectors-status
```

Implemented artifacts:

- `data_runtime/com_offers_case_profiles.jsonl`;
- `data_runtime/com_offers_case_profile_progress.json`;
- `data_runtime/com_offers_case_profile_vectors.json`;
- `data_runtime/com_offers_case_profile_vector_progress.json`.

## Evaluation

Use only benchmark data from:

- `com_offers/tests/ground truth.xlsx`

Metrics:

- Hit@1;
- Hit@3;
- Hit@5;
- Hit@10;
- MRR;
- Precision@5;
- Recall@5;
- nDCG@5;
- nDCG@10.

Compare modes on the same 87-query benchmark:

- lexical/exact fallback;
- semantic-only;
- lexical-only;
- current hybrid;
- profile semantic-only;
- hybrid + profile;
- hybrid + profile + reranker.

Current evaluation command surface:

```bash
python3 tools/evaluate_com_offers_ground_truth.py --mode fallback
python3 tools/evaluate_com_offers_ground_truth.py --mode lexical
python3 tools/evaluate_com_offers_ground_truth.py --mode semantic
python3 tools/evaluate_com_offers_ground_truth.py --mode profile
MRO_KB_PROFILE_SEARCH_ENABLED=1 python3 tools/evaluate_com_offers_ground_truth.py --mode hybrid-profile
```

Acceptance for enabling profile retrieval by default:

- Hit@5 improves by at least 0.05 over current hybrid baseline;
- Hit@10 does not regress;
- exact identifier queries do not regress;
- ambiguous documents are still not treated as trusted evidence;
- median latency remains acceptable for OpenWebUI after profiles are prebuilt.

## Baseline Checkpoint

Current measured baseline before profile retrieval:

- fallback + `Reestr_zayavok`: Hit@5 `0.644`, Hit@10 `0.678`;
- partial semantic min-max hybrid before Reestr reindex: Hit@5 `0.690`, Hit@10 `0.747`;
- reranker not yet included in these metrics.

Before enabling the profile path, rebuild embeddings with Reestr enrichment and record fresh current-hybrid metrics.
