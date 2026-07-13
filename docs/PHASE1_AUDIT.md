# Phase 1 Audit

This audit checks the implemented project against the current Phase 1 scope:

- standalone `MRO KB` only
- no `com_offers`
- ingest from `MRO_RAG`
- SQLite runtime store
- OpenWebUI adapter
- Obsidian publish links

## Implemented

- New standalone project in `mro_kb_platform/`
- Single-source ingest from `MRO_RAG/apps/webapp/demo_data`
- SQLite runtime store for cases, documents, chunks, links, and snapshots
- Obsidian note publishing for cases and documents
- OpenWebUI pipeline adapter in `apps/openwebui_pipeline/pipeline.py`
- Runtime API:
  - `GET /api/health`
  - `GET /api/cases`
  - `GET /api/cases/{case_id}`
  - `GET /api/documents/{document_id}`
  - `GET /api/chunks/{chunk_id}`
  - `POST /api/chat`
  - `POST /api/ingest/run`
- Retrieval-only chat with citations and Obsidian URIs

## Intentionally deferred

- `com_offers` structured case layer
- rules-based `Decision Engine`
- price / effort estimation
- dense vector index and reranker
- page image extraction and `page_assets` population
- AP-25 retrieval integration

## Result

The implemented project matches the reduced Phase 1 target:

- it is a standalone MRO knowledge base;
- it does not depend on `com_offers`;
- it uses `MRO_RAG` as the source corpus;
- it exposes OpenWebUI-compatible chat;
- it links sources to Obsidian notes.

It does **not** yet match the larger multi-stage target that also includes:

- decision support,
- pricing,
- richer indexing,
- page images.
