# Architecture

## Layers

### MRO KB

- document corpus from `MRO_RAG/apps/webapp/demo_data`
- retrieval over cases, chunks, and tables
- OpenWebUI-facing chat output with source citations

### Obsidian publish layer

- generated vault under `data_runtime/obsidian_vault`
- one note per case
- one note per document
- stable block ids for chunks

## Runtime truth

- SQLite database under `data_runtime/mro_kb.sqlite3`
- generated indexes and vault notes are derived artifacts
