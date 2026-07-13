# mro_kb_platform

`mro_kb_platform` is a standalone runtime project for:

- MRO knowledge-base chat over completed case documents;
- OpenWebUI integration;
- Obsidian publish links for human source review.

The project intentionally separates:

- `MRO KB` retrieval and evidence generation;
- `Obsidian` as a secondary publish surface, not as runtime truth.

## Sources

- `MRO_RAG` supplies case-first document trees and chunks.
- `Intranet_RAG-main` is the integration reference for OpenWebUI-style pipelines.

## Quick start

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_kb_platform
python3 -m apps.api.server --host 127.0.0.1 --port 8120
```

Then run ingest:

```bash
curl -X POST http://127.0.0.1:8120/api/ingest/run
```

To test with the same live components as `Intranet_RAG`:

```bash
export MRO_KB_RERANKER_URL=http://10.251.10.5:9101
export MRO_KB_LLM_BASE_URL=http://10.100.112.71:1234/v1
export MRO_KB_LLM_API_KEY=local
python3 -m apps.api.server --host 127.0.0.1 --port 8120
```

`MRO_KB_RERANKER_MODEL` defaults to `BAAI/bge-reranker-v2-m3`.
`MRO_KB_LLM_PROVIDER` defaults to `openai`.
`MRO_KB_LLM_MODEL` can stay empty and will auto-resolve through `/v1/models`.

## Main endpoints

- `GET /api/health`
- `GET /api/cases`
- `GET /api/cases/{case_id}`
- `GET /api/documents/{document_id}`
- `GET /api/chunks/{chunk_id}`
- `POST /api/chat`
- `POST /api/ingest/run`
