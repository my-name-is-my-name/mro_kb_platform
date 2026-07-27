# ATA Impact v2: отчёт о критических исправлениях

Дата: 2026-07-27

Репозиторий: `my-name-is-my-name/mro_kb_platform`

Базовый commit: `023e61db6efe19e83fbd37707632c9012b7d8e17`

Текущий HEAD: `023e61db6efe19e83fbd37707632c9012b7d8e17`

Изменения находятся в working tree. Новый commit не создавался, поскольку `.git`
в рабочем окружении доступен только для чтения.

## Итог

Кодовые acceptance gates выполнены:

- `BLOCKER = 0`;
- `MAJOR = 0`;
- production flow использует отдельный critic call;
- все mapper candidates имеют уникальный `candidate_id`;
- critic coverage проверяется детерминированно;
- отсутствие verdict не создаёт affected ATA;
- `require_document` не входит в affected ATA;
- document confirmation реализован как state transition;
- Go/No-Go не проходит без affected ATA;
- context-only, uncertainties и required inputs блокируют assessment;
- unknown runtime mode не открывает legacy pipeline;
- `installed_in` считается location relation;
- controlled evidence проверяется строго;
- reasoning-style embedded JSON не принимается production parser;
- HTTP contracts унифицированы;
- document references и certificate subchapters исправлены;
- полный test suite проходит;
- новый независимый final reviewer дал `PASS`.

Production semantic readiness не заявляется. Real-LLM semantic suite завершилась
fail-safe fallback на fact extraction и не подтвердила семантическую стабильность
на реальной модели.

## Предварительный анализ findings

Все заявленные findings были подтверждены на базовом commit.

### BLOCKER 1. Critic не был независимым

Подтверждено.

Где находилось:

- `core/ata_impact/service.py`;
- `core/ata_impact/prompts.py`.

Механизм:

- standard mode мог использовать `ATA_MAPPING_AND_CRITIC_PROMPT`;
- mapping и critic формировались одной генерацией;
- результат этой генерации фактически назывался independent critic, хотя нового
  LLM-контекста не было.

Regression:

- `test_standard_and_auto_always_use_independent_critic`;
- проверки extended mode и trace.

Исправление:

- во всех production modes выполняются отдельные calls:

```text
engineering facts
→ ATA mapping
→ certificate validation
→ independent critic
```

- critic получает только validated mapping candidates и certificate validation;
- combined prompt оставлен только как deprecated experimental constant;
- production service его не импортирует и не вызывает.

### BLOCKER 2 и MAJOR 7. Candidate без critic verdict попадал в affected

Подтверждено.

Где находилось:

- `core/ata_impact/validator.py`;
- deterministic assembly в `core/ata_impact/service.py`.

Механизм:

- object candidate мог автоматически стать `inferred_from_request`;
- structural candidate мог автоматически стать `direct_confirmed`;
- ATA/category использовались вместо уникальной candidate identity;
- пропущенные, duplicate и неизвестные critic actions не обеспечивали полной
  fail-safe coverage.

Regression:

- missing critic action;
- empty critic;
- duplicate actions;
- malformed плюс valid duplicate;
- unknown candidate ID;
- category/ATA mismatch;
- candidate ID с неверным anchor или ATA token;
- одинаковая ATA для разных entities;
- одинаковая ATA и entity с разными основаниями;
- collision critic-added candidate.

Исправление:

- mapping создаёт только `candidate_unverified`;
- каждый candidate получает уникальный ID:

```text
candidate:<category>:<entity-or-relation-or-request>:<ata>:<sequence>
```

- ID проверяется на соответствие фактическим category, anchor и ATA;
- critic action обязан ссылаться на exact `candidate_id`;
- каждый candidate должен иметь ровно один action;
- missing, duplicate, unknown, malformed или mismatched action оставляет
  candidate unverified и блокирует completion;
- critic additions проходят тот же основной validator;
- raw ID collision отклоняется до нормализации.

### BLOCKER 3. `require_document` сохранял affected state

Подтверждено.

Где находилось:

- `core/ata_impact/validator.py`;
- compatibility assembly.

Механизм:

- `require_document` не удалял `direct_confirmed` или
  `inferred_from_request`;
- candidate мог одновременно выглядеть affected и требующим документа.

Regression:

- object candidate плюс `require_document`;
- structural candidate плюс `require_document`;
- possible interface плюс document confirmation;
- wrong candidate ID;
- non-applicable document;
- obsolete revision;
- anchor mutation;
- correct document transition;
- source-fragment-only procedure record.

Исправление:

- добавлен status `document_verification_required`;
- он входит только в `potentially_affected_ata`;
- controlled confirmation удаляет старый status;
- candidate появляется только в `document_confirmed`;
- provenance сохраняет `previous_status=document_verification_required`;
- source fragment не является достаточным document anchor.

### MAJOR 1. Go/No-Go мог пройти без affected ATA

Подтверждено.

Где находилось:

- `core/go_no_go.py`.

Механизм:

- решение опиралось преимущественно на старый `_missing_inputs()`;
- context-only и staged unresolved states учитывались неполно;
- empty affected мог перейти к assessment.

Regression:

- empty affected ATA;
- context-only ATA;
- required input data;
- uncertainty;
- document verification required;
- potential ATA;
- certificate unavailable;
- out-of-certificate ATA;
- valid closed case;
- unresolved user-declared ATA.

Исправление:

- Go/No-Go использует полный staged output;
- `go_to_assessment` разрешён только при непустом affected ATA и полностью
  закрытых inputs, uncertainties, potential, document, certificate и coverage
  gates;
- context-only возвращает `hold_expert_review`;
- empty affected возвращает `need_more_info` либо `hold_expert_review`;
- out-of-certificate ATA не превращается автоматически в NO_GO.

### MAJOR 2. `completed` возвращался при незакрытом анализе

Подтверждено.

Где находилось:

- `core/ata_impact/service.py`.

Механизм:

- `required_input_data` вычислялся, но не блокировал decision;
- uncertainties, evidence completeness и potential states учитывались неполно;
- `retriever.status=completed` ошибочно мог восприниматься как наличие
  подтверждающего документа;
- user-declared unresolved ATA не участвовала в gate.

Regression:

- affected плюс uncertainty;
- affected плюс required input;
- affected плюс empty documents;
- document required, но retriever вернул zero documents;
- critical warning;
- fully closed completed case;
- affected ATA плюс отдельная unverified user-declared ATA.

Исправление:

- `completed` разрешён только при полностью закрытом analysis state;
- unresolved user-declared ATA добавляет
  `user_declared_ata_unresolved`;
- evidence stage и наличие exact document confirmation проверяются раздельно;
- decision states разделены на:

```text
completed
completed_with_hypotheses
engineering_review_required
additional_input_required
document_verification_required
```

### MAJOR 3. `installed_in` ошибочно создавал interface

Подтверждено.

Где находилось:

- relation validation в `core/ata_impact/validator.py`;
- interface selection в `core/ata_impact/service.py`.

Механизм:

- location relation могла использоваться как interface basis;
- часть решений зависела от свободного текста reason.

Regression:

- equipment installed in cargo compartment;
- installed-in pipeline context-only;
- attached-to frame;
- removal required;
- access through another system;
- protection of adjacent equipment;
- electrical connection;
- adjacent relation без structured basis.

Исправление:

- `installed_in` и `location_reference` запрещены как interface basis;
- `adjacent_to` требует structured `interface_basis`:
  `access_required` или `protection_required`;
- interface требует реальной structured relation.

### MAJOR 4. Неизвестный mode открывал legacy

Подтверждено.

Где находилось:

- ATA agent runtime selection;
- dedicated и main HTTP endpoints.

Механизм:

- typo вроде `standrd` попадал в legacy `ontology_llm` branch;
- runtime modes проверялись в нескольких местах по-разному.

Regression:

- empty mode;
- `standrd`;
- uppercase/mixed case;
- whitespace;
- valid `auto`, `standard`, `extended`;
- legacy mode с feature flag и без него.

Исправление:

- добавлен общий `core/ata_impact/modes.py`;
- production allowlist:

```text
auto
standard
extended
```

- legacy allowlist:

```text
rules_only
ontology_llm
full_pipeline
```

- legacy разрешён только при exact mode и
  `MRO_KB_ENABLE_LEGACY_ATA_MODES=true`;
- unknown и mixed-case modes возвращают `400 invalid_request_error` либо
  `ValueError`.

### MAJOR 5. Controlled evidence проверялся недостаточно строго

Подтверждено.

Где находилось:

- `core/ata_impact/evidence.py`;
- document confirmation;
- compatibility output;
- Go/No-Go evidence projection.

Механизм:

- trust level был достаточен для попадания в compatibility evidence;
- metadata completeness и exact candidate identity проверялись неполно;
- production retriever не получал request-scoped candidate IDs.

Regression:

- missing/whitespace document metadata;
- wrong ATA/category/anchor/candidate ID;
- missing record-level verification;
- obsolete revision;
- non-applicable document;
- source-fragment-only record;
- candidate-aware production adapter.

Исправление:

- введена одна строгая функция `is_controlled_evidence_document()`;
- обязательны:

```text
trust_level = controlled_oem | approved_data
applicable = true
current_revision = true
verification_status = confirmed
document_id
document_type
revision
effectivity
section_reference
confirmed_candidates with exact candidate_id
entity_id or relation_id anchor
record verification_status = confirmed
confirmed_claim
```

- `retrieved_documents` и `controlled_evidence` разделены;
- retriever contract получает exact validated `mapping_candidates` с
  request-scoped IDs, categories и anchors;
- current internal corpus не self-confirms candidates.

### MAJOR 6. Production parser принимал embedded reasoning JSON

Подтверждено.

Где находилось:

- LLM response parsing в `core/ata_impact/service.py`;
- runtime client.

Механизм:

- из reasoning-style content выбирался один из нескольких JSON objects;
- использовался heuristic scoring;
- partial/truncated response мог восстанавливаться.

Regression:

- reasoning плюс intermediate/final JSON;
- два JSON объекта;
- fenced JSON;
- empty response;
- array вместо object;
- successful repair;
- failed repair;
- `finish_reason=length`.

Исправление:

- production parser принимает только:

```text
whole response JSON object
official structured-output object
one clean fenced JSON block
```

- выполняется максимум один repair request;
- repair получает stage и validation errors;
- truncated response не восстанавливается;
- raw chain-of-thought не сохраняется.

### MINOR 1. HTTP fields и request aliases имели разный precedence

Подтверждено.

Где находилось:

- `apps/api/ata_server.py`;
- `apps/api/server.py`.

Механизм:

- main и dedicated endpoints по-разному объединяли flat/nested fields;
- `request`, `question` и `q` имели разный порядок приоритета.

Исправление:

- добавлен общий `core/ata_impact/http_contract.py`;
- conflicting flat/nested field возвращает HTTP 400;
- conflicting non-empty request aliases возвращают HTTP 400;
- одинаковые aliases принимаются;
- stream обязан быть boolean;
- agent exception возвращается как JSON `server_error`.

### MINOR 2. Document references разбирались неполно

Подтверждено.

Где находилось:

- `core/ata_impact/identifiers.py`.

Поддержаны:

```text
AMM ATA 34-11
AMM 34-11
SRM 53-10-01
IPC ATA 25-50
CMM 32-10-15
WDM 24-00
NTM 51-00
ALS ATA 05-10
```

ATA внутри document reference больше не считается user-declared ATA.

### MINOR 3. Certificate subchapter match был неточным

Подтверждено.

Где находилось:

- `core/ata_impact/certificate_validator.py`;
- compatibility `CertificateCatalog.match()` в `core/go_no_go.py`.

Исправление:

- сначала выбирается exact normalized ATA;
- chapter fallback применяется только при отсутствии subchapter;
- missing subchapter возвращает `ambiguous_subchapter`;
- случайная запись другой подглавы не прикрепляется;
- unavailable catalog возвращает `catalog_unavailable`.

## Дополнительные проблемы, найденные review cycles

### Потеря нескольких explicit ATA в fallback

Architecture reviewer обнаружил, что fallback candidates не имели ID и
дедуплицировались по пустому ключу.

Пример:

```text
ATA 25 + ATA 34
→ validated fallback сохранял только ATA 25
```

Исправлено:

- каждый fallback candidate получает deterministic unique ID;
- обе ATA сохраняются в `user_declared_unverified`;
- affected остаётся пустым;
- decision остаётся `engineering_review_required`;
- lexical certificate matching полностью удалён из production v2 fallback.

### Unverified user-declared ATA не блокировала completed

Первый final reviewer обнаружил:

```text
confirmed ATA 25 + unverified declared ATA 34
→ decision completed
→ возможен go_to_assessment
```

Исправлено в service decision и Go/No-Go. Незакрытая declared ATA теперь всегда
блокирует completion и assessment.

### Source-fragment-only document confirmation

Первый final reviewer обнаружил, что procedure candidate мог подтвердиться
документом, содержащим только совпадающий `source_fragment`.

Исправлено:

- mapping factual anchor по source fragment сохранён;
- document confirmation требует entity или relation anchor;
- source-fragment-only record остаётся в
  `document_verification_required`.

## Candidate state machine

Начальное состояние mapper:

```text
candidate_unverified
```

Переходы object/structural:

```text
confirm
→ inferred_from_request | direct_confirmed

reject
→ rejected

downgrade_to_location_context
→ location_context

downgrade_to_possible
→ possible_interface, только при валидной relation

require_document
→ document_verification_required
```

Переходы interface/procedure:

```text
confirm
→ possible_interface | possible_procedure

require_document
→ document_verification_required
```

Document transition:

```text
document_verification_required
→ exact applicable controlled confirmation
→ document_confirmed
```

Mapping-origin `candidate_state=candidate_unverified` сохраняется в provenance.
Authoritative post-critic state задаётся validated bucket и полем `status`.

## Independent critic

Trace production flow содержит отдельные шаги:

```text
engineering_fact_extraction
ata_mapping
certificate_scope_validation
independent_critic
oem_document_verification
```

Critic:

- работает в новом LLM-контексте;
- не получает скрытые рассуждения mapper;
- получает validated mapping;
- получает certificate validation;
- обязан дать ровно один verdict на candidate;
- не может изменить ATA/category/anchor обычным action;
- добавление missing candidate проходит основной validator;
- collision или unknown ID invalidates coverage.

## Go/No-Go gates

`go_to_assessment` разрешён только если:

- `affected_ata` не пуст;
- result не context-only;
- `potentially_affected_ata` пуст;
- `required_input_data` пуст;
- uncertainties пусты;
- `document_verification_required` пуст;
- `candidate_unverified` пуст;
- `user_declared_unverified` пуст;
- critical warnings отсутствуют;
- staged decision равен `completed`;
- certificate catalog загружен;
- certificate status однозначен.

Certificate match остаётся предварительным scope check и не является capability
approval.

## LLM repair flow

```text
initial response
→ strict parse/schema validation
→ one repair request with stage and validation errors
→ strict re-validation
→ fail-safe fallback after second failure
```

При `finish_reason=length`:

```text
status = repair_failed
reason = truncated_response
```

Partial JSON recovery не выполняется.

## Изменённые файлы

### API

- `apps/api/ata_server.py`;
- `apps/api/server.py`.

### ATA Impact core

- `core/ata_impact/certificate_validator.py`;
- `core/ata_impact/evidence.py`;
- `core/ata_impact/http_contract.py` — новый;
- `core/ata_impact/identifiers.py`;
- `core/ata_impact/modes.py` — новый;
- `core/ata_impact/models.py`;
- `core/ata_impact/prompts.py`;
- `core/ata_impact/schemas.py`;
- `core/ata_impact/service.py`;
- `core/ata_impact/validator.py`.

### Integrations

- `core/go_no_go.py`;
- `core/runtime_clients.py`.

### Documentation and scripts

- `docs/ATA_IMPACT_AGENT.md`;
- `scripts/evaluate_ata_impact_benchmark.py`.

### Tests

- `tests/test_ata_http_contract.py`;
- `tests/test_ata_impact_agent.py`;
- `tests/test_ata_impact_safety_regressions.py` — новый;
- `tests/test_ata_impact_v1.py`;
- `tests/test_ata_impact_v2.py`;
- `tests/test_go_no_go.py`;
- `tests/test_main_ata_http_contract.py` — новый.

Всего: 23 изменённых или созданных файла.

## Результаты тестов

### Полный test discovery

Команда:

```bash
python3.13 -m unittest discover -s tests -p 'test_*.py' -v
```

Результат:

```text
Ran 122 tests
OK (skipped=3)
```

Skips:

- два live socket test classes блокируются sandbox;
- real-LLM integration является opt-in.

### Native HTTP, OpenAI-compatible и SSE

Команда выполнена вне socket sandbox:

```bash
python3.13 -m unittest \
  tests.test_ata_http_contract \
  tests.test_main_ata_http_contract -v
```

Результат:

```text
Ran 15 tests
OK
```

Проверены:

- dedicated native ATA endpoint;
- main native ATA endpoint;
- OpenAI non-stream envelope;
- SSE envelope и `[DONE]`;
- mode forwarding;
- invalid mode;
- legacy feature flag;
- flat/nested conflicts;
- request alias conflicts;
- invalid stream type;
- JSON error contracts;
- agent exception contract.

### Статические проверки

```text
python3.13 -m compileall core apps tests
PASS

git diff --check
PASS

JSON configs
2/2 valid

production hardcode scan
clean

legacy/combined prompt production scan
clean
```

GitHub Actions workflows отсутствуют. CI result получить невозможно.

## Real LLM semantic suite

Endpoint был доступен при предварительной проверке, после чего был запущен
semantic matrix:

- roller track context;
- roller track plus structural attachment;
- static pressure port proximity;
- static pressure port removal;
- context-only request;
- multi-object request.

Каждый сценарий был запланирован на три повтора.

Итог:

```text
Ran 1 test in 1050.710s
FAILED
```

В первом проверяемом roller-track scenario все три результата:

```text
affected = []
potential = []
context = []
schema_success = false
repair_count = 0
fallback_count = 1
mapping_candidates = []
critic_actions = []
final_statuses = {}
finish_reason = null
response_truncation = false
```

Сервис fail-safe ушёл в fallback и не создал ложную affected ATA. Однако schema
и semantic success не подтверждены. Из-за fail на первой проверке test report не
напечатал telemetry остальных scenarios.

Вывод:

- safety fallback работает;
- production semantic readiness не подтверждена;
- нельзя заявлять готовность real-LLM pipeline до успешного повторного matrix
  run.

## Review/fix cycles

### Cycle 1

Reviewers проверяли architecture, MRO semantics и regression contracts.

Найдены и исправлены:

- critic-added candidate ID collision;
- extra unknown critic action;
- `require_document` anchor mutation;
- слабая evidence identity validation;
- implicit record verification;
- free-text adjacent interface basis;
- whitespace document metadata;
- malformed плюс valid duplicate actions;
- schema/repair trace gaps;
- critical evidence warning gates;
- compatibility document-required state;
- API socketless error handling;
- documentation mismatch.

Verdict после fixes:

```text
PASS AFTER FIXES
BLOCKER = 0
MAJOR = 0
```

### Cycle 2

Найден MAJOR:

- fallback терял вторую и последующие explicit ATA из-за пустых candidate IDs.

Также исправлены:

- candidate ID не был связан с actual anchor/ATA;
- compatibility certificate matching matrix;
- документация controlled evidence.

Verdict после fixes:

```text
CYCLE-2 REVIEW VERDICT: PASS
BLOCKER = 0
MAJOR = 0
```

### Первый final review

Первый отдельный final reviewer дал:

```text
FINAL REVIEW VERDICT: FAIL
```

Найдены три MAJOR:

- unresolved user-declared ATA не блокировала completed/assessment;
- production evidence integration не получала request-scoped candidates;
- source-fragment-only document record мог подтвердить procedure candidate.

Найдены MINOR:

- inconsistent deterministic candidate state;
- разные HTTP request alias precedence;
- lexical fallback diagnostics в production v2.

Все findings исправлены. После этого полный test suite был выполнен повторно.

### Новый final review

После fixes был запущен новый reviewer, не участвовавший в реализации.

Его полный verdict:

```text
FINAL REVIEW VERDICT: PASS

BLOCKER:
- Нет.

MAJOR:
- Нет.

MINOR:
- В validated output сохраняется mapper-origin
  candidate_state="candidate_unverified" рядом с authoritative post-critic
  status. На safety-логику это не влияет, но контракт требует пояснения для
  потребителей.

ACCEPTED_RISK:
- Real LLM matrix 6×3 завершилась fail-safe fallback на facts stage;
  production semantic readiness не подтверждена.
- Текущий production evidence backend не формирует candidate-bound confirmation
  records самостоятельно. До подключения controlled verifier document-required
  candidates безопасно остаются незакрытыми.
- Deprecated combined prompt и legacy embedded-JSON parser остаются только в
  явно feature-flagged legacy/experimental flow.
```

Все 18 обязательных final safety gates reviewer подтвердил.

## Оставшиеся MINOR и accepted risks

### MINOR

Validated item сохраняет первоначальный mapper state
`candidate_state=candidate_unverified`, тогда как authoritative состояние после
critic задаётся полем `status` и validated bucket. Это сделано для provenance,
но должно быть явно учтено потребителями API.

### Accepted risks

1. Real-LLM semantic readiness не подтверждена.
2. Текущий internal evidence backend только ищет документы и передаёт exact
   mapping candidates в verifier contract, но сам не создаёт controlled
   candidate-bound confirmation records.
3. До подключения такого verifier `document_verification_required` остаётся
   безопасно незакрытым.
4. Deprecated combined prompt сохранён как experimental constant, но не
   используется production flow.
5. Legacy embedded-JSON logic доступна только в явно feature-flagged legacy
   pipeline.
6. GitHub Actions configuration отсутствует.

## Git diff

Tracked files:

```text
19 files changed, 1631 insertions(+), 354 deletions(-)
```

Новые untracked файлы:

```text
core/ata_impact/http_contract.py
core/ata_impact/modes.py
tests/test_ata_impact_safety_regressions.py
tests/test_main_ata_http_contract.py
```

В новых файлах суммарно 1308 строк на момент финальной проверки.

## Команды повторного запуска

Полный suite:

```bash
python3.13 -m unittest discover -s tests -p 'test_*.py' -v
```

HTTP/OpenAI/SSE:

```bash
python3.13 -m unittest \
  tests.test_ata_http_contract \
  tests.test_main_ata_http_contract -v
```

Targeted ATA и Go/No-Go:

```bash
python3.13 -m unittest \
  tests.test_ata_impact_v2 \
  tests.test_ata_impact_safety_regressions \
  tests.test_go_no_go -v
```

Compile и diff:

```bash
python3.13 -m compileall core apps tests
git diff --check
```

Real LLM:

```bash
MRO_KB_RUN_ATA_LLM_INTEGRATION=1 \
python3.13 -m unittest \
  tests.test_ata_impact_v2.RealLLMIntegrationTests.test_repeated_semantic_matrix \
  -v
```

## Финальный статус

Кодовые safety и regression gates пройдены. Независимый final reviewer дал
`PASS`, открытых BLOCKER или MAJOR нет.

Production readiness не заявляется до успешного real-LLM semantic matrix и
подключения controlled evidence verifier, способного выпускать exact
candidate-bound confirmation records.
