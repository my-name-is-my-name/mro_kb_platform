# Operations Runbook

## Start API

Recommended OpenWebUI API port for this project:

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_kb_platform
mkdir -p ~/.config/systemd/user
cp deploy/systemd/mro-kb-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mro-kb-api.service
loginctl enable-linger "$USER"
```

OpenWebUI base URL:

```text
http://10.100.112.51:8121/v1

The independent ATA-impact endpoint for OpenWebUI is:

```text
http://10.100.112.51:8122/v1
```

It publishes model `mro-ata-impact`. Install and enable
`deploy/systemd/mro-ata-impact.service` for a persistent deployment. The service
does not replace or restart the legacy API on port 8121.
```

Do not run another project on the same port.

Check the persistent service:

```bash
systemctl --user status mro-kb-api.service
```

View logs:

```bash
journalctl --user -u mro-kb-api.service -f
```

Stop it with:

```bash
systemctl --user stop mro-kb-api.service
```

Fallback for environments without user systemd:

```bash
tools/start_mro_api.sh
tools/stop_mro_api.sh
```

The fallback start script uses `nohup`, writes the PID to `data_runtime/mro_api_8121.pid`, and appends logs to `data_runtime/mro_api_8121.log`.

## Check Health

```bash
curl http://127.0.0.1:8121/api/health
```

Important fields:

- `commercial_offers.converted_markdown.ready`
- `commercial_offers.converted_markdown.cases`
- `commercial_offers.converted_markdown.documents`
- `commercial_offers.converted_markdown.conflicts`
- `commercial_offers.case_embeddings.ready`
- `commercial_offers.case_embeddings.count`
- `commercial_offers.warnings`
- `commercial_offers.reranker`
- `commercial_offers.llm`

If `case_embeddings.ready=false`, semantic search is not active and quality is fallback-only.

## Build Converted Markdown Manifest

Use this after `converted_md` or `converted_md_pdf_ocr` changes:

```bash
MRO_KB_LLM_ENABLED=0 MRO_KB_RERANKER_ENABLED=0 python3 -m apps.api.server rebuild-com-offers-manifest
```

Expected output includes:

- `cases`
- `documents`
- `skipped`
- `conflicts`
- `cache`
- `seconds`

Current known baseline:

- `cases`: 951
- `documents`: 1188
- `skipped`: 316
- `conflicts`: 7

The manifest file is:

```text
data_runtime/com_offers_converted_markdown_manifest.json
```

## Rebuild Commercial Embeddings

Use after `case_registry.csv` or the converted Markdown manifest changes:

```bash
MRO_KB_LLM_ENABLED=0 MRO_KB_RERANKER_ENABLED=0 python3 -m apps.api.server reindex-com-offers
```

Requirements:

- Ollama endpoint from `MRO_KB_OLLAMA_URL`, default `http://127.0.0.1:11434`;
- embedding model `bge-m3:latest`, unless `MRO_KB_EMBEDDING_MODEL` is changed.

The embedding cache is:

```text
data_runtime/com_offers_case_vectors.json
```

Reindex can take several minutes. API startup does not rebuild embeddings automatically.

Reindex is resumable. Existing vectors are reused when their `text_hash` still matches the current case-card text. Progress is written to:

```text
data_runtime/com_offers_reindex_progress.json
```

Check progress without starting another reindex:

```bash
python3 -m apps.api.server reindex-com-offers-status
```

Important progress fields:

- `status`: `running`, `partial`, `complete`, `missing`, or `unreadable`;
- `total`: registry case count;
- `done`: currently available vectors;
- `reused`: vectors reused from the previous cache;
- `built`: vectors built in the current run;
- `failure_count`: failed embedding cases;
- `current_case_id`: last processed case.

## Build Experimental Case Profiles

LLM case profiles are experimental and are not used by default ranking yet.

Run a small smoke build first:

```bash
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles --limit 5
```

Build all profiles after the smoke output is acceptable:

```bash
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles
```

Force refresh:

```bash
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles --force
```

The profile cache is:

```text
data_runtime/com_offers_case_profiles.jsonl
```

Progress is written to:

```text
data_runtime/com_offers_case_profile_progress.json
```

Check profile status:

```bash
python3 -m apps.api.server com-offer-profiles-status
```

The profile builder uses only production sources already loaded into the case card. It does not read benchmark answers.

Build profile embeddings after profiles exist:

```bash
python3 -m apps.api.server reindex-com-offer-profile-vectors
```

Check profile embedding status:

```bash
python3 -m apps.api.server com-offer-profile-vectors-status
```

Profile retrieval is disabled by default. Enable it only for controlled tests:

```bash
export MRO_KB_PROFILE_SEARCH_ENABLED=1
```

Evaluate modes independently:

```bash
python3 tools/evaluate_com_offers_ground_truth.py --mode fallback
python3 tools/evaluate_com_offers_ground_truth.py --mode lexical
python3 tools/evaluate_com_offers_ground_truth.py --mode semantic
python3 tools/evaluate_com_offers_ground_truth.py --mode profile
MRO_KB_PROFILE_SEARCH_ENABLED=1 python3 tools/evaluate_com_offers_ground_truth.py --mode hybrid-profile
```

## Ingest `mro-kb`

Use this when the MRO_RAG source data changes:

```bash
curl -X POST http://127.0.0.1:8121/api/ingest/run
```

This rebuilds SQLite document/case tables and publishes Obsidian notes.

## OpenWebUI Configuration

Add an OpenAI-compatible connection:

```text
Base URL: http://10.100.112.51:8121/v1
API key: any non-empty value if OpenWebUI requires one
```

Models should appear as:

- `mro-kb`
- `mro-similar-cases`

If models do not appear:

1. Check `GET /v1/models`.
2. Check OpenWebUI connection URL includes `/v1`.
3. Check the API process is running on `8121`.
4. Make sure another service is not occupying the same port.

## Runtime Options

Disable LLM answer generation:

```bash
export MRO_KB_LLM_ENABLED=0
```

Disable reranker:

```bash
export MRO_KB_RERANKER_ENABLED=0
```

Enable reranker:

```bash
export MRO_KB_RERANKER_ENABLED=1
export MRO_KB_RERANKER_URL=http://10.251.10.5:9101
```

Enable LLM final answer generation:

```bash
export MRO_KB_LLM_ENABLED=1
export MRO_KB_LLM_BASE_URL=http://10.100.112.71:1234/v1
export MRO_KB_LLM_API_KEY=local
```

Enable experimental query rewrite:

```bash
export MRO_KB_QUERY_REWRITE_ENABLED=1
```

Query rewrite is disabled by default. Do not use it to inject correct case IDs or manual aliases.

## Troubleshooting

### `commercial_offer_converted_markdown_manifest_missing_run_reindex_com_offers`

Run:

```bash
python3 -m apps.api.server rebuild-com-offers-manifest
```

### `commercial_offer_embedding_index_stale_registry_changed_run_reindex_com_offers`

Run:

```bash
python3 -m apps.api.server reindex-com-offers
```

Check progress:

```bash
python3 -m apps.api.server reindex-com-offers-status
```

### Slow startup

Startup should only read JSON artifacts. If startup scans Markdown, the code has regressed. The API must not call full converted Markdown scanning during normal `serve`.

### Wrong document evidence

Check link status in the answer:

- `matched`: usable if readable and same case family;
- `ambiguous_match`: not trusted;
- `missing_document`: similarity came from registry/manifest only;
- `document_link_mismatch`: not trusted;
- `unreadable_document`: not trusted.

### Poor query quality

Check in this order:

1. Does `case_registry.csv` contain enough description?
2. Does `converted_md_pdf_ocr` contain the missing terms?
3. Is the manifest current?
4. Is the embedding index current?
5. Is reranker enabled?

Do not fix quality by adding query-specific hardcode.

### LLM profile experiment

LLM-generated case profiles are experimental. They should be built from production sources only:

- `case_registry.csv`;
- `Reestr_zayavok.xlsm`;
- converted Markdown/OCR manifests;
- reviewed compact extraction from MinerU Markdown, if enabled later.

Do not use `com_offers/tests/ground truth.xlsx` for profile generation. It is benchmark data only.
