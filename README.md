# mro_kb_platform

`mro_kb_platform` is a standalone runtime for two OpenWebUI models:

- `mro-kb`: RAG over completed MRO case documents and engineering evidence.
- `mro-similar-cases`: analogue search over commercial MRO requests from `com_offers`.
- `mro-go-no-go`: first-line recommendation whether a new request can proceed to engineering assessment, needs more information, is out of scope, or requires expert review.

The two models are intentionally separate. `mro-kb` answers from the engineering/document knowledge base. `mro-similar-cases` searches commercial analogues and uses documents only as evidence, not as the primary similarity source.

## Data Sources

Runtime source roots:

- project: `/mnt/ii_models/Users/hizhenkov/mro_kb_platform`
- commercial offers: `/mnt/ii_models/Users/hizhenkov/com_offers`
- MRO RAG source data: `/mnt/ii_models/Users/hizhenkov/MRO_RAG`

Main `mro-similar-cases` inputs:

- `com_offers/pilot_artifacts/case_registry.csv`: primary case registry and main source of similarity.
- `com_offers/converted_md_pdf_ocr`: converted presentation Markdown/OCR used to enrich searchable case cards.
- `com_offers/converted_md`: secondary converted Markdown source; currently sparse.
- `com_offers/pilot_artifacts/case_document_links.csv`: document-link quality layer.
- `com_offers/pilot_artifacts/case_documents.jsonl`: normalized document metadata.

Generated runtime artifacts:

- `data_runtime/com_offers_converted_markdown_manifest.json`
- `data_runtime/com_offers_case_vectors.json`
- `data_runtime/mro_kb.sqlite3`
- `data_runtime/obsidian_vault`

Generated artifacts can be rebuilt. Do not treat them as source truth.

## Quick Start

Build the commercial Markdown enrichment manifest:

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_kb_platform
MRO_KB_LLM_ENABLED=0 MRO_KB_RERANKER_ENABLED=0 python3 -m apps.api.server rebuild-com-offers-manifest
```

Build commercial case embeddings when Ollama `bge-m3:latest` is available:

```bash
MRO_KB_LLM_ENABLED=0 MRO_KB_RERANKER_ENABLED=0 python3 -m apps.api.server reindex-com-offers
```

Start the API:

```bash
python3 -m apps.api.server --host 0.0.0.0 --port 8121
```

OpenWebUI base URL:

```text
http://10.100.112.51:8121/v1

The ATA-impact service is deployed independently at:

```text
http://10.100.112.51:8122/v1
```

Select the `mro-ata-impact` model in an OpenAI-compatible client. ATA impact v2
extracts engineering facts before ATA classification, lets the LLM create
candidates, validates certificate scope separately, runs a critic, and then
queries the configured OEM evidence retriever. Certificate scope is not
capability approval. The legacy ontology and keyword catalog are disabled by
default and remain only in explicit deprecated compatibility modes.
Reasoning-style local models may require `MRO_KB_ATA_LLM_MAX_TOKENS` (default
`4000`) and `MRO_KB_ATA_LLM_TIMEOUT_SECONDS` (default `90`) so the structured
JSON answer is not truncated.
```

Available OpenWebUI models:

- `mro-kb`
- `mro-similar-cases`

## Runtime Commands

`serve` starts the OpenAI-compatible API:

```bash
python3 -m apps.api.server --host 0.0.0.0 --port 8121
```

`rebuild-com-offers-manifest` scans converted Markdown and writes a compact manifest. It does not call embeddings:

```bash
python3 -m apps.api.server rebuild-com-offers-manifest
```

`reindex-com-offers` rebuilds the converted Markdown manifest and then rebuilds case-card embeddings:

```bash
python3 -m apps.api.server reindex-com-offers
```

`publish-com-offer-registry` writes one Markdown dashboard page per commercial request. Similar-case answers link case IDs to browser-friendly HTML pages through `/api/com-offers/registry/{case_id}`; raw Markdown is available with `?format=md`:

```bash
python3 -m apps.api.server publish-com-offer-registry
```

`build-com-offer-profiles` builds experimental LLM case profiles. Profile vectors are not used by default ranking until metrics justify enabling them. The runtime still emits structured diagnostics from available profiles/fallback profiles: `structured_score`, `similarity_reason_class`, `go_no_go`, and `cost_readiness`.

```bash
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles --limit 5
python3 -m apps.api.server com-offer-profiles-status
python3 -m apps.api.server reindex-com-offer-profile-vectors
python3 -m apps.api.server com-offer-profile-vectors-status
```

`POST /api/ingest/run` ingests the `mro-kb` document corpus from `MRO_RAG` into SQLite and publishes Obsidian notes:

```bash
curl -X POST http://127.0.0.1:8121/api/ingest/run
```

## Main Endpoints

- `GET /api/health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /api/chat`
- `POST /api/ingest/run`
- `GET /api/cases`
- `GET /api/cases/{case_id}`
- `GET /api/documents/{document_id}`
- `GET /api/chunks/{chunk_id}`
- `GET /api/com-offers/registry/{case_id}`
- `POST /api/triage`

Example go/no-go request:

```bash
curl -X POST http://127.0.0.1:8121/api/triage \
  -H 'Content-Type: application/json' \
  -d '{"request":"Ремонт повреждения фюзеляжа ATA 53 на Airbus A320"}'
```

The triage response is a recommendation and always contains `needs_human_approval: true`. T-Search is an optional evidence retriever; configure `MRO_KB_TSEARCH_ENABLED=1` and `MRO_KB_TSEARCH_URL` only after deploying its separate harness. See [T-Search deployment specification](docs/T_SEARCH_DEPLOYMENT_SPEC.md).

## Runtime Environment

Optional reranker:

```bash
export MRO_KB_RERANKER_URL=http://10.251.10.5:9101
export MRO_KB_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

Optional LLM for final answer generation:

```bash
export MRO_KB_LLM_BASE_URL=http://10.100.112.71:1234/v1
export MRO_KB_LLM_API_KEY=local
export MRO_KB_LLM_PROVIDER=openai
```

LLM query rewrite is disabled by default. Enable only for experiments:

```bash
export MRO_KB_QUERY_REWRITE_ENABLED=1
```

Do not put query-specific mappings, case IDs, manual aliases, or Russian case-form substitutions into runtime code or prompts. Retrieval quality must come from source data, converted Markdown, embeddings, exact identifiers, and reranking.

## Current Status

As of the latest implementation:

- converted Markdown manifest is built from real `converted_md*` files;
- manifest contains 951 cases and 1188 Markdown documents;
- 316 files are skipped by filters;
- 7 conflicting paths are reported instead of indexed silently;
- semantic embeddings must be rebuilt after manifest changes.

Check current status with:

```bash
curl http://127.0.0.1:8121/api/health
```

If `case_embeddings.ready` is `false`, `mro-similar-cases` falls back to lexical/exact retrieval and quality will be lower than semantic mode.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Search Logic](docs/MRO_SIMILAR_CASES_SEARCH_LOGIC.md)
- [Evaluation](docs/MRO_SIMILAR_CASES_EVALUATION.md)
- [LLM Profile Experiment](docs/LLM_PROFILE_EXPERIMENT_PLAN.md)
- [Operations](docs/OPERATIONS.md)
