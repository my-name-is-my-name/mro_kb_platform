# MRO Request Assessment

`mro-request-assessment` is a standalone orchestration service for explainable preliminary airline request assessment.

It calls external components only through HTTP:

- `MRO_ASSESSMENT_ATA_URL`, default `http://127.0.0.1:8122/api/ata-impact`
- `MRO_ASSESSMENT_MRO_KB_URL`, default `http://127.0.0.1:8121/api/chat`
- `MRO_ASSESSMENT_SIMILAR_CASES_URL`, default `http://127.0.0.1:8121/api/similar-cases/search`

Optional configuration:

- `MRO_ASSESSMENT_HTTP_TIMEOUT_SECONDS`, default `10`
- `MRO_ASSESSMENT_SIMILAR_CASES_FALLBACK_ENABLED`, default `false`
- `MRO_ASSESSMENT_MAX_RAG_CASES`, default `3`
- `MRO_ASSESSMENT_CAPABILITY_REGISTRY`, default `config/request_assessment_capabilities.json`
- `MRO_ASSESSMENT_DB_PATH`, default `data_runtime/request_assessment.sqlite3`

Runtime requirements:

- Python `>=3.12,<3.13`
- `pydantic>=2,<3`

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

Capability registry:

- The default `config/request_assessment_capabilities.json` is intentionally `UNAVAILABLE` and contains no production capability.
- `examples/request_assessment_capabilities.example.json` is demonstration data only. Do not use it as a production capability source without expert verification and controlled document approval.
- Registry modes:
  - `UNAVAILABLE`: capability is `UNKNOWN`; automatic `ACCEPT_FOR_QUOTATION` and `DECLINE` are forbidden.
  - `ADVISORY`: results are context only; automatic `ACCEPT_FOR_QUOTATION` and `DECLINE` are forbidden.
  - `CONTROLLED`: deterministic `ACCEPT_FOR_QUOTATION` or `DECLINE` is allowed only when all strict checks pass.

Decision logic is deterministic:

1. confirmed CONTROLLED capability or approval hard fail -> `DECLINE`
2. blocking customer-provided missing information -> `REQUEST_INFORMATION`
3. registry mode is not `CONTROLLED` -> `EXPERT_REVIEW`
4. unresolved capability, approval or documentary uncertainty -> `EXPERT_REVIEW`
5. passed mandatory controlled checks -> `ACCEPT_FOR_QUOTATION`

Similar cases embedded in the `mro-ata-impact` response are reused. The fallback `/api/similar-cases/search` client runs only when the embedded block is missing, disabled or unavailable and fallback is explicitly enabled.

Historical RAG workflow:

1. `mro-ata-impact` structures the new request and may embed Similar Cases.
2. `mro-request-assessment` selects the top `MRO_ASSESSMENT_MAX_RAG_CASES` technically useful candidates across all Similar Cases groups. Historical accepted/not accepted status does not directly drive the decision.
3. For every selected `case_id`, `mro-request-assessment` sends an addressed HTTP query to `mro-kb` asking for document-backed facts about prior work, even when aircraft type/MSN are still missing. If no qualified analogs are found, it performs one wide MRO KB search for partial historical materials.
4. Blocking missing customer data keeps the formal decision conservative (`REQUEST_INFORMATION`) and defers capability/approval checks, but it does not discard historical inference returned by MRO KB.
5. The service extracts a universal `HistoricalFact` list using best effort parsing: explicit structured fields first, then a valid JSON block in `answer`, then explicit metadata from `sources`/`evidence`. Invalid JSON is a warning, not a workflow failure.
6. The service builds `historical_inference` with `historical_support`, `proposed_scope`, `differences`, `assumptions` and `missing_inputs`.

Supported historical fact categories are intentionally small:

- `activity`
- `calculation`
- `document`
- `customer_input`
- `discipline`
- `reference`
- `constraint`
- `outcome`

Historical support values:

- `CANDIDATES_ONLY`: Similar Cases candidates are available, but MRO KB document verification has not run because the current routing policy determined it is not required or unavailable for that path.
- `DIRECT`: a strong Similar Case and document-backed facts about prior work are available.
- `PARTIAL`: partial historical materials are available but do not confirm a full analog work package.
- `NONE`: no useful document-backed historical facts were extracted.
- `UNAVAILABLE`: MRO KB was unavailable; absence of historical documents is not inferred.

`quotation_readiness` is separate from the formal decision:

- `NEEDS_INFORMATION`: blocking customer data is missing.
- `READY_FOR_ESTIMATION`: direct historical support exists and proposed scope is not empty.
- `NEEDS_EXPERT_REVIEW`: support is partial/none/unavailable or expert confirmation is otherwise needed.

Historical RAG never becomes automatic capability proof. The deterministic decision engine remains conservative: controlled hard fail -> `DECLINE`; blocking missing data -> `REQUEST_INFORMATION`; capability/approval/documentary uncertainty -> `EXPERT_REVIEW`; controlled checks pass -> `ACCEPT_FOR_QUOTATION`.

MRO KB HTTP success is not treated as documentary confirmation. Returned `sources` and `evidence` are normalized and assessed for matching `case_id`, document/chunk identifiers, snippet presence, relevance and applicability. Empty or inconclusive RAG results never imply `DECLINE`; if documentary verification is required, they route the request to `EXPERT_REVIEW`.

OpenWebUI streaming emits safe operational `reasoning_content`, including service names, safe endpoints, query payloads, counts and gate outcomes. Secret-like fields are recursively removed before streaming.
