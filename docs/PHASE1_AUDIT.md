# Phase 1 Audit

This file is historical.

The original Phase 1 scope covered only:

- standalone `mro-kb`;
- ingest from `MRO_RAG`;
- SQLite runtime store;
- OpenWebUI-compatible chat;
- Obsidian source links.

That scope was implemented.

The project has since expanded and now also includes:

- `mro-similar-cases`;
- commercial analogue search over `com_offers`;
- converted Markdown manifest enrichment;
- commercial case embedding cache;
- OpenWebUI exposure of both `mro-kb` and `mro-similar-cases`.

For current architecture and operation, use:

- [Architecture](ARCHITECTURE.md)
- [Search Logic](MRO_SIMILAR_CASES_SEARCH_LOGIC.md)
- [Operations](OPERATIONS.md)
- [Evaluation](MRO_SIMILAR_CASES_EVALUATION.md)
