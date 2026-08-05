# MRO Request Assessment

`mro-request-assessment` is a standalone orchestration service for explainable preliminary airline request assessment.

It calls external components only through HTTP:

- `MRO_ASSESSMENT_ATA_URL`, default `http://127.0.0.1:8122/api/ata-impact`
- `MRO_ASSESSMENT_MRO_KB_URL`, default `http://127.0.0.1:8121/api/chat`
- `MRO_ASSESSMENT_SIMILAR_CASES_URL`, default `http://127.0.0.1:8121/api/similar-cases/search`

Optional configuration:

- `MRO_ASSESSMENT_HTTP_TIMEOUT_SECONDS`, default `10`
- `MRO_ASSESSMENT_SIMILAR_CASES_FALLBACK_ENABLED`, default `false`
- `MRO_ASSESSMENT_CAPABILITY_REGISTRY`, default `config/request_assessment_capabilities.json`
- `MRO_ASSESSMENT_DB_PATH`, default `data_runtime/request_assessment.sqlite3`

Run:

```bash
python -m apps.request_assessment.server --host 127.0.0.1 --port 8123
```

Endpoints:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /api/assessments`
- `GET /api/assessments/{request_id}`
- `POST /api/assessments/{request_id}/answers`
- `POST /api/assessments/{request_id}/review`

The OpenAI-compatible model id is `mro-request-assessment`. Streaming uses SSE chunks with safe `reasoning_content` operational progress and a final `content` chunk.

Decision logic is deterministic:

1. confirmed capability or approval hard fail -> `DECLINE`
2. blocking customer-provided missing information -> `REQUEST_INFORMATION`
3. unresolved capability, approval or documentary uncertainty -> `EXPERT_REVIEW`
4. passed mandatory checks -> `ACCEPT_FOR_QUOTATION`

Similar cases embedded in the `mro-ata-impact` response are reused. The fallback `/api/similar-cases/search` client runs only when the embedded block is missing, disabled or unavailable and fallback is explicitly enabled.

