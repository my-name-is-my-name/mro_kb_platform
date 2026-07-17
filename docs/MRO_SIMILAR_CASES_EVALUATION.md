# mro-similar-cases Evaluation

Date: 2026-07-17

This document records the current evaluation state for `mro-similar-cases`.

## Current Runtime State

Latest checked artifacts:

- converted Markdown manifest: ready
- manifest file: `data_runtime/com_offers_converted_markdown_manifest.json`
- manifest cases: 951
- manifest documents: 1188
- skipped converted Markdown files: 316
- conflicting converted Markdown paths: 7
- embedding cache after reindex: partial, 1216 vectors for 1246 registry cases
- embedding missing cases reported by `/api/health`: `MP-0048`, `MP-0071`, `MP-0089`, `MP-0109`, `MP-0343`, `MP-0405`, `MP-0462`, `MP-0664`, `MP-1040`
- reranker endpoint checked separately: `BAAI/bge-reranker-v2-m3`, CUDA enabled

The current branch includes structured case-profile diagnostics, compact rerank text and go/no-go/cost-readiness output. Structured profile score is not used as a direct ranking boost because the measured boost degraded top-N quality on the benchmark.

Fallback evaluation is still useful as a lower-bound check and is run with:

- `MRO_KB_LLM_ENABLED=0`
- `MRO_KB_RERANKER_ENABLED=0`
- semantic vectors disabled/stale
- retrieval path: lexical + exact identifiers + converted Markdown manifest + `Reestr_zayavok.xlsm` enrichment

This is not the target production quality mode. It is a lower-bound check that verifies the system still works without semantic embeddings.

## Ground Truth Sources

Primary benchmark source:

- `/mnt/ii_models/Users/hizhenkov/com_offers/tests/ground truth.xlsx`
- sheet `Поиск заявки`: user-style search queries
- sheet `Заявки`: expected commercial request IDs and normalized descriptions

The evaluation script builds expected answers automatically by matching each query from `Поиск заявки` to descriptions from `Заявки`. This file is evaluation data only. It is not used by runtime retrieval, so it does not create query-to-case hardcode.

Evaluation command:

```bash
python3 tools/evaluate_com_offers_ground_truth.py --disable-vectors --json-out data_runtime/com_offers_ground_truth_fallback_report.json
```

The script expands base expected IDs to registry variants for scoring. Example: if the Excel file says `MP-0856` but the registry contains `MP-0856.01` and `MP-0856.02`, either concrete registry case can count as relevant. This is a general evaluation rule, not a retrieval signal.

## Benchmark Validity

The benchmark contains 87 queries with expected IDs. The expected IDs are limited to the historical slice up to base request `918`:

- expected IDs above `918`: 0;
- rows with multiple expected IDs: 21;
- expected IDs missing after base/subcase expansion: 0;
- registry currently contains newer cases above `918`: 259.

For honest regression tracking, the main MVP metric should be computed on the temporal slice `--max-case-number 918`. Running against the full registry is still useful as a robustness check because newer cases are realistic distractors, but it is not the same distribution as the benchmark labels.

Known benchmark/data limitation: expected case `MP-0109` is one of the embedding failures in the current partial vector cache, so semantic retrieval cannot fully recover that row until the vector failure is fixed.

## Smoke Ground Truth Set

| Query | Expected case(s) |
|---|---|
| `АМОС ДЛГ Шпангоута 35.3` | `MP-0861` |
| `Анализ ПКМ для стоек шасси А320` | `MP-0135` |
| `Вмятины верхних хвостовиков предкрылков` | `MP-0819` |
| `Возможность выполнения технического перегона` | `MP-0738` |
| `Временная установка макетных стоек шасси` | `MP-0128` |
| `Временное разрешение на эксплуатацию с трещиной` | `MP-0842`, `MP-0842.01` |
| `Вывешивание ВС для проведения ремонтных работ` | `MP-0184`, `MP-0197`, `MP-0209`, `MP-0215`, `MP-0225`, `MP-0239`, `MP-0330`, `MP-0523`, `MP-0618` |
| `Замена огнетушителей` | `MP-0172.2` |
| `Затяжка шпилек крепления киля к фюзеляжу` | `MP-0079`, `MP-0079.1`, `MP-0079.2`, `MP-0339` |
| `Изменение ограничения выполнения директивы летной годности` | `MP-0764` |

## Production Enrichment Sources

Runtime retrieval must not read expected answers from the benchmark workbook.

Allowed production enrichment sources:

- `case_registry.csv`: primary registry;
- `Reestr_zayavok.xlsm`, sheet `Реестр`: additional case descriptions, BD comments, tasks, notes, and folder names;
- converted Markdown/OCR manifests from `converted_md*`;
- future compact profiles generated from real registry/document content.

Not allowed as runtime enrichment:

- `com_offers/tests/ground truth.xlsx`;
- manual `query -> case_id` mappings;
- manually curated Russian case-form aliases.

## Latest Results

The latest honest snapshot uses the temporal benchmark slice up to `MP-0918`, no query-to-case hardcode, no manual aliases, and no hand-written synonym/case-form dictionaries.

Fresh semantic hybrid, reranker disabled:

| Metric | Value |
|---|---:|
| Hit@1 | 0.460 |
| Hit@3 | 0.655 |
| Hit@5 | 0.690 |
| Hit@10 | 0.793 |
| MRR | 0.567 |
| Precision@5 | 0.177 |
| Recall@5 | 0.617 |
| nDCG@5 | 0.539 |
| Precision@10 | 0.112 |
| Recall@10 | 0.741 |
| nDCG@10 | 0.581 |
| CostUsable@5 | 0.618 |
| TrustedEvidence@5 | 0.584 |

Semantic hybrid with compact external reranker, controlled temporal benchmark:

| Pool | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR | nDCG@10 |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 0.529 | 0.713 | 0.736 | 0.805 | 0.623 | 0.635 |
| 100 | 0.529 | 0.701 | 0.736 | 0.828 | 0.622 | 0.642 |

Current result is still below the MVP target `Hit@5 >= 0.80` and `Hit@10 >= 0.90`. The main blocker is candidate generation, not only final ranking: candidate-pool recall@100 on the temporal slice is `0.885`.

Candidate-pool misses at top-100 on the temporal slice:

| Row | Query | Expected |
|---:|---|---|
| 11 | `Временное разрешение на эксплуатацию с трещиной` | `MP-0842` |
| 16 | `Инцидент с обледенением` | `MP-0057` |
| 25 | `Модификация дренажной системы` | `MP-0109` |
| 29 | `Перевод ремонта узла подвески вертикального стабилизатора из категории B в категорию А` | `MP-0771` |
| 30 | `Перенос кислородных баллонов` | `MP-0503` |
| 40 | `Ремонт внутренней панели реверса тяги` | `MP-0825` |
| 42 | `Ремонт капота двигателя` | `MP-0460` |
| 47 | `Ремонт коррозии выреза под люк на крыле` | `MP-0694` |
| 55 | `Ремонт коррозии порожка` | `MP-0478` |
| 59 | `Ремонт крепления аккумуляторной батареи` | `MP-0490` |

For rows where the expected analogue enters the candidate pool, compact reranking is useful. With pool 100, reranked Hit@10 is `0.828` overall; on the retrievable subset this is materially higher, but the 10 pool misses cap the final MVP metrics.

Historical fallback-only run after removing manual token normalization and manual aliases:

Small 10-query smoke set:

| Metric | Value |
|---|---:|
| Hit@1 | 0.50 |
| Hit@3 | 0.60 |
| Hit@5 | 0.60 |
| Hit@10 | 0.70 |
| MRR | 0.546 |

Full Excel benchmark, 87 queries, base ID expansion enabled:

| Metric | Value |
|---|---:|
| Hit@1 | 0.402 |
| Hit@3 | 0.598 |
| Hit@5 | 0.632 |
| Hit@10 | 0.678 |
| MRR | 0.499 |
| Precision@5 | 0.156 |
| Recall@5 | 0.534 |
| nDCG@5 | 0.468 |
| Precision@10 | 0.094 |
| Recall@10 | 0.610 |
| nDCG@10 | 0.492 |

Fallback with `Reestr_zayavok.xlsm` enrichment, 87 queries, base ID expansion enabled:

| Metric | Value |
|---|---:|
| Hit@1 | 0.414 |
| Hit@3 | 0.552 |
| Hit@5 | 0.644 |
| Hit@10 | 0.678 |
| MRR | 0.497 |
| Precision@5 | 0.159 |
| Recall@5 | 0.545 |
| nDCG@5 | 0.468 |
| Precision@10 | 0.093 |
| Recall@10 | 0.603 |
| nDCG@10 | 0.486 |

Partial semantic benchmark with min-max score fusion, 87 queries, 1217/1246 vectors loaded, reranker disabled:

| Metric | Value |
|---|---:|
| Hit@1 | 0.414 |
| Hit@3 | 0.644 |
| Hit@5 | 0.690 |
| Hit@10 | 0.747 |
| MRR | 0.525 |
| Precision@5 | 0.170 |
| Recall@5 | 0.603 |
| nDCG@5 | 0.509 |
| Precision@10 | 0.101 |
| Recall@10 | 0.680 |
| nDCG@10 | 0.533 |

Important: fallback numbers are expected to be lower than target because semantic embeddings are disabled there.
The partial semantic result confirms that embeddings are useful after score normalization, but still not enough for the target MVP quality on current case cards.

Additional MRO-ready evaluation metrics are now emitted by `tools/evaluate_com_offers_ground_truth.py`:

- `candidate_recall_at_50`;
- `candidate_recall_at_100`;
- `cost_usable_at_5`;
- `trusted_evidence_at_5`.

`candidate_recall_at_50/100` checks whether the expected analogue is present in a wide candidate pool before final top-N use. This shows whether reranking or structured profile ranking can still recover a case.

`cost_usable_at_5` is not a price metric. It measures whether top-5 analogues have enough historical signals to be used as future cost comparables.

`trusted_evidence_at_5` measures whether top-5 analogues have at least one trusted evidence document.

The next quality checks should:

- rebuild embeddings after `Reestr_zayavok.xlsm` enrichment;
- compare lexical-only, semantic-only, and hybrid metrics on the same 87-query benchmark;
- test the GPU reranker only after the candidate pool contains the correct case often enough;
- evaluate structured MRO profiles from production source text;
- review `misses_candidate_pool_top100` separately from top-10 misses.

Candidate-pool evaluation intentionally requests up to top-100 results per query and is slower than the old top-10 benchmark. Use it for benchmark snapshots, not for every smoke check.

Do not enrich runtime retrieval from the Excel `Заявки` sheet in `ground truth.xlsx`; that sheet is benchmark data.

## Observed Per-Query Behavior

Good in fallback mode:

- `Вмятины верхних хвостовиков предкрылков` -> `MP-0819` rank 1
- `Временная установка макетных стоек шасси` -> `MP-0128` rank 1
- `Вывешивание ВС для проведения ремонтных работ` -> relevant group rank 1
- `Замена огнетушителей` -> `MP-0172.2` rank 1
- `Затяжка шпилек крепления киля к фюзеляжу` -> relevant group rank 1

Weak in fallback mode:

- `АМОС ДЛГ Шпангоута 35.3`
- `Анализ ПКМ для стоек шасси А320`
- `Временное разрешение на эксплуатацию с трещиной`
- `Изменение ограничения выполнения директивы летной годности`

Additional weak groups from the full Excel benchmark:

- short generic modification/repair queries where lexical overlap is too broad;
- component names mostly present in Excel descriptions but sparse in `case_registry.csv`;
- queries where the useful signal is in converted Markdown/OCR and requires semantic matching;
- cases where the Excel expected ID is a base request and the registry contains only sub-requests.

These weak cases depend on semantic matching between Russian query wording and English/OCR/domain terms in converted Markdown.

## Converted Markdown Data Quality

Independent review found:

- `converted_md` is sparse and not useful for most test cases.
- `converted_md_pdf_ocr` is useful and contains the needed search terms for most cases.
- OCR noise exists, especially in figure/table-heavy slides, but key terms usually survive.
- Main quality risk is wrong-case pollution from nested example/archive/reference folders.

The manifest builder now addresses this by:

- skipping example/archive/reference/tmp/прочность paths;
- rejecting conflicting case IDs in a path;
- indexing compact extracted search text, not full OCR dumps.

Known weak data:

- `MP-0339` primary converted document is very short and noisy.
- Some useful related docs may be excluded if they have conflicting case IDs; this is intentional until they are manually reviewed.

## MinerU Markdown Review

`/mnt/ii_models/Users/mineru/output` contains cleaner converted Markdown for some requests, but the files are often much more verbose than the user queries and include drawings, procedures, tables, and unrelated appendix text.

The current decision is:

- do not dump full MinerU Markdown into case embeddings;
- use it only after adding compact extraction/profile generation;
- keep compact fields focused on problem, component, zone, defect, action, identifiers, and constraints.

## Target Production Criteria

After a successful `reindex-com-offers` with fresh embeddings:

- `Hit@5 >= 0.80`
- `Hit@10 >= 0.90`
- no correct answer should disappear from the expanded candidate pool
- exact identifiers such as `AD`, `SB`, `FR`, `RIB`, `P/N`, `MSN`, registration must remain visible in reasons
- ambiguous/missing/unreadable documents must not be shown as trusted sources

## Next Required Evaluation

Run:

```bash
MRO_KB_LLM_ENABLED=0 MRO_KB_RERANKER_ENABLED=0 python3 -m apps.api.server reindex-com-offers
python3 -m unittest tests.test_commercial_offers_similarity tests.test_mro_import_linking
python3 tools/evaluate_com_offers_ground_truth.py --json-out data_runtime/com_offers_ground_truth_semantic_report.json
```

Then update this file with semantic-mode metrics from the 87-query Excel benchmark.
