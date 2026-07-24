# Architecture

`mro_kb_platform` exposes two OpenWebUI-compatible models from one API process.

## Models

### `mro-kb`

Purpose:

- answer questions over completed MRO cases and engineering documents;
- provide source citations;
- publish source material into an Obsidian vault for human review.

Main source:

- `/mnt/ii_models/Users/hizhenkov/MRO_RAG/apps/webapp/demo_data`

Runtime storage:

- `data_runtime/mro_kb.sqlite3`
- `data_runtime/obsidian_vault`

Implementation:

- `core/retrieval/service.py`
- `storage/sqlite/store.py`
- `ingest/mro_docs/import_documents.py`
- `ingest/publish_obsidian/publish.py`

### `mro-similar-cases`

Purpose:

- find analogous commercial MRO requests;
- show why each case is similar;
- show what must be checked;
- show document evidence only when document linkage is reliable;
- explicitly state that the result is analogue search, not price calculation and not a final go/no-go decision.

Main source:

- `/mnt/ii_models/Users/hizhenkov/com_offers/pilot_artifacts/case_registry.csv`

Enrichment sources:

- `/mnt/ii_models/Users/hizhenkov/com_offers/Reestr_zayavok.xlsm`
- `/mnt/ii_models/Users/hizhenkov/com_offers/converted_md`
- `/mnt/ii_models/Users/hizhenkov/com_offers/converted_md_pdf_ocr`

Evidence sources:

- `/mnt/ii_models/Users/hizhenkov/com_offers/pilot_artifacts/case_document_links.csv`
- `/mnt/ii_models/Users/hizhenkov/com_offers/pilot_artifacts/case_documents.jsonl`

Runtime artifacts:

- `data_runtime/com_offers_converted_markdown_manifest.json`
- `data_runtime/com_offers_case_vectors.json`

Implementation:

- `core/commercial_offers.py`

## API Layer

Entrypoint:

- `apps/api/server.py`

OpenAI-compatible endpoints:

- `GET /v1/models`
- `POST /v1/chat/completions`

ATA impact v2 находится в `core/ata_impact/`. Engineering fact extraction, ATA
mapping, certificate validation, critic, OEM evidence retrieval и deterministic
assembly имеют отдельные границы. `CertificateCatalog` предоставляет официальный
certificate scope, но не доказывает техническую классификацию и capability.

`config/mro_ontology_v1.json` и `config/ata_catalog_overrides.json` deprecated и
disabled by default. Они доступны только через explicit compatibility modes и
не передаются v2 LLM как allowlist.

Native endpoints:

- `GET /api/health`
- `POST /api/chat`
- `POST /api/ingest/run`
- `GET /api/cases`
- `GET /api/cases/{case_id}`
- `GET /api/documents/{document_id}`
- `GET /api/chunks/{chunk_id}`

`/v1/models` returns:

- `mro-kb`
- `mro-similar-cases`

## Commercial Similarity Pipeline

The commercial pipeline is intentionally data-driven.

At query time:

1. The user query is used as-is by default.
2. No manual Russian case-form substitutions are applied.
3. No query-specific aliases are applied.
4. Semantic, lexical, and exact-identifier candidates are merged.
5. Optional reranker reorders final candidates.
6. Evidence documents are attached only after source-quality checks.

At index time:

1. `rebuild-com-offers-manifest` scans converted Markdown.
2. It writes a compact manifest with strict `case_id` linkage.
3. `CommercialOffersService` reads `Reestr_zayavok.xlsm` as production enrichment.
4. `reindex-com-offers` builds case-card embeddings from registry + Reestr + manifest text.
5. API startup reads existing artifacts and never silently rebuilds them.

## Converted Markdown Manifest

The manifest exists to avoid scanning 1500+ Markdown files at API startup.

Manifest builder rules:

- read only `converted_md` and `converted_md_pdf_ocr`;
- identify `case_id` from the nearest case folder or main file name;
- ignore range folders like `МР 101-200`;
- skip nested example/archive/reference/tmp/прочность paths;
- reject paths where folder and filename contain conflicting case IDs;
- keep compact search text, not full OCR dumps.

The manifest currently records:

- case count;
- document count;
- skipped file count;
- conflict count;
- per-case compact search text;
- conflict samples for audit.

## Embedding Index

The commercial embedding index is stored in:

- `data_runtime/com_offers_case_vectors.json`

It is built by:

```bash
python3 -m apps.api.server reindex-com-offers
```

The API checks:

- embedding model;
- registry hash;
- per-case text hash.

If the cache is stale or incomplete, `/api/health` reports a warning and semantic retrieval is disabled until reindex finishes.

## Evidence Layer

Evidence is not the primary source of similarity.

Trusted sources must satisfy:

- link status is `matched`;
- path/document id matches the same case family;
- file is readable;
- document is selected for the query.

Untrusted statuses are shown as warnings:

- `ambiguous_match`;
- `missing_document`;
- `document_link_mismatch`;
- `unreadable_document`.

Only trusted documents are emitted in top-level `sources`.

## Design Constraints

- `mro-kb` and `mro-similar-cases` must stay separate.
- `case_registry.csv` remains the main source for commercial analogue search.
- Converted Markdown enriches searchable case cards but is not a final truth source.
- `Reestr_zayavok.xlsm` is allowed as production enrichment; benchmark Excel files under `com_offers/tests` are not.
- Aircraft type is display/check metadata, not a primary retrieval signal.
- Query-specific hardcode is not allowed.
- Manual `case_search_aliases.csv` is not used.
