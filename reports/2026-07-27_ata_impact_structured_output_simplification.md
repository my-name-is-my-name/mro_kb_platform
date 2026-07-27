# ATA Impact structured-output simplification — 2026-07-27

## Идентификация

- Base SHA: `c8c77d95d9250e5fd3bc9078d58835f1b04322f7`
- Base subject: `Harden ATA Impact v2 safety pipeline`
- Final HEAD: commit, содержащий этот отчёт; точный SHA фиксируется в итоговом handoff после commit.
- Scope: ATA Impact v2, его OpenAI-compatible structured transport, непосредственная
  интеграция staged ATA result с Go/No-Go, тесты и ATA-документация.

## Краткий итог

- Facts, mapping и independent critic сохранены как три отдельных production-вызова.
- Для каждой стадии endpoint получает отдельную portable JSON Schema через
  `response_format`; режим и факт server enforcement отражаются в trace.
- `reasoning_content` не используется как ответ и не попадает в trace.
- Mapping-модель больше не генерирует `candidate_id`; идентификаторы назначает Python
  после deterministic validation.
- Critic coverage остаётся fail-safe: отсутствующий, повторный или неизвестный verdict
  не создаёт affected ATA.
- Go/No-Go использует staged engineering facts и готовое technical evidence, не выполняя
  второй технический LLM extraction или повторный technical retrieval.
- User-declared ATA reconciled как consistent/conflicting/unverified/not-in-certificate.
- `installed_in`, `part_of` и необоснованный `adjacent_to` не создают interface.
- Critical warning policy централизована в ATA validation gate.

## Архитектура до и после

До:

```text
facts text generation
→ local parsing/schema check
→ mapping text generation with model candidate_id
→ critic
→ ATA evidence retrieval
→ Go/No-Go technical fact enrichment
→ repeated Go/No-Go technical evidence retrieval
```

После:

```text
HTTP normalization
→ formal identifiers
→ structured facts + deterministic validation
→ structured mapping + deterministic validation
→ Python candidate_id allocation
→ certificate validation
→ independent structured critic + exact coverage
→ deterministic state assembly
→ one technical evidence verification pass
→ Go/No-Go consumes staged facts, gates and evidence
```

## Structured LLM transport

`OpenAICompatibleLLM.structured_chat()` — единственная новая transport abstraction.
Она возвращает компактный `StructuredLLMResponse` и различает valid response, empty
content, truncation, schema error, transport error, repair success/failure.

Поддержаны профили:

- `json_schema`: strict adapter, `schema_enforced_by_server=true`;
- `json_schema_no_strict`: canonical schema без strict, enforcement не заявляется;
- `json_object`: JSON-object transport, полная локальная validation;
- `prompt_only`: enforcement не заявляется, полная локальная validation;
- неизвестный профиль: fail-safe до network call.

Strict adapter изменяет только transport-копию схемы. Каноническая MRO-схема общая.
Repair выполняется один раз как отдельная стадия `json_repair`, получает validation
errors и исходную business schema. Partial/truncated и произвольный embedded JSON не
восстанавливаются.

## Candidate identity и state assembly

Mapper возвращает только ATA, category-specific anchor, confidence и reason. После
validation Python детерминированно назначает:

```text
candidate:<category>:<normalized-anchor>:<ata-token>:<sequence>
```

Sequence и глобальный allocator исключают collision для одинаковых ATA, разных entities
и разных инженерных оснований. Публичный candidate содержит:

```text
initial_state = candidate_unverified
status = <текущий детерминированный статус>
```

Critic ссылается только на `candidate_id` и не может изменить ATA/category/anchor.
`require_document` переводит candidate в `document_verification_required`; только exact
candidate-bound applicable/current/confirmed controlled record выполняет заменяющий
переход в `document_confirmed`.

## Go/No-Go gates

`go_to_assessment` требует одновременно:

- непустой `affected_ata`;
- отсутствие potential/document-required ATA;
- отсутствие required input и engineering uncertainties;
- неcritical staged validation gate;
- закрытый ATA decision и доступный однозначный certificate scope;
- отсутствие blocking user-declared ATA state.

Context-only и empty-affected cases блокируются. Go/No-Go не переинтерпретирует warning
prefixes и не изменяет ATA mapping через evidence.

## Удалённые дублирующие пути

- удалён production combined mapping+critic path;
- удалена обязанность mapper генерировать orchestration `candidate_id`;
- удалён `GoNoGoService._llm_enrich_facts()` и unsafe embedded-JSON extraction;
- удалён второй Go/No-Go technical document retrieval;
- удалена передача полного certificate catalog в mapper;
- общая interface-relation policy используется mapper validation и critic transitions;
- authoritative critical-warning gate формируется ATA service.

## Изменённые файлы

| Файл | Причина | Тип | Влияние вне ATA | Подтверждающий тест |
|---|---|---|---|---|
| `core/runtime_clients.py` | ATA-only structured transport/profile | Production | отсутствует; generic `chat()` неизменён | `test_openai_transport_profiles_and_generic_chat_isolation` |
| `core/ata_impact/models.py` | публичные user-declared reconciliation statuses | Production | отсутствует | ATA v2 suite |
| `core/ata_impact/prompts.py` | mapping без ID, structured critic/repair | Production | отсутствует | structured stage tests |
| `core/ata_impact/schemas.py` | portable stage schemas | Production | отсутствует | schema regression tests |
| `core/ata_impact/service.py` | structured stages, repair, trace, staged gate | Production | отсутствует | ATA v2/safety suites |
| `core/ata_impact/validator.py` | Python IDs, coverage, reconciliation, relation policy | Production | отсутствует | candidate/MRO safety tests |
| `core/go_no_go.py` | reuse staged facts/evidence and gates | Production | отсутствует вне ATA integration | Go/No-Go suite |
| `tests/test_ata_impact_v2.py` | updated model contract and scenarios | Test | отсутствует | self |
| `tests/test_ata_impact_safety_regressions.py` | transport, IDs, repair, safety regressions | Test | отсутствует | self |
| `tests/test_go_no_go.py` | no duplicate processing and gate regressions | Test | отсутствует | self |
| `docs/ATA_IMPACT_AGENT.md` | current production contract | Docs | отсутствует | review |
| `reports/2026-07-27_ata_impact_v2_critical_fixes.md` | historical-snapshot banner | Docs | отсутствует | scope review |
| `reports/2026-07-27_ata_impact_structured_output_simplification.md` | compact final evidence | Docs | отсутствует | scope review |

Изменённых файлов вне разрешённой области нет. `apps/api/*` не изменены: существующая
нормализация и mode validation уже соответствовали контракту.

## Размер diff

До добавления этого компактного отчёта:

- ATA production lines added: 1013
- ATA production lines removed: 380
- ATA test lines added: 808
- ATA test lines removed: 70
- Documentation lines added/removed: 38/3
- Unrelated production lines changed: 0
- Новые зависимости: 0

Большая часть добавлений — portable schema definitions и regression tests. Framework,
registry, event bus, state-machine dependency или provider class hierarchy не добавлялись.

## Проверки

Targeted:

```text
python3.13 -m unittest \
  tests.test_ata_impact_v2 \
  tests.test_ata_impact_safety_regressions \
  tests.test_go_no_go \
  tests.test_ata_http_contract \
  tests.test_main_ata_http_contract -v

Ran 105 tests in 4.720s — OK (skipped=3 in restricted sandbox)
```

Полный project discovery:

```text
python3.13 -m unittest discover -s tests -p 'test_*.py' -v
Ran 141 tests in 42.465s — OK (skipped=3)
```

Native HTTP/OpenAI/SSE вне socket sandbox:

```text
python3.13 -m unittest \
  tests.test_ata_http_contract tests.test_main_ata_http_contract -v
Ran 15 tests in 1.021s — OK
```

Static/config:

- `python3.13 -m compileall core apps tests` — PASS
- `git diff --check` — PASS
- оба tracked JSON config прошли `python3.13 -m json.tool`
- combined prompt / ATA reasoning fallback / embedded slicing production scan — clean
- `.github/workflows` отсутствует; GitHub Actions result не заявляется

## Real-LLM status

Выполнены 6 сценариев × 3 повтора с endpoint-моделью
`qwen3.6-35b-a3b@q4_k_m`, profile `json_schema`. Endpoint ответил `finish_reason=stop`,
но оставил `content` пустым и вернул только `reasoning_content`, в том числе на repair.
Это запрещённый контракт: facts не прошёл, downstream mapping/critic не запускались,
каждый запрос завершился fail-safe fallback. Chain-of-thought не сохранён.

Safety fallback подтверждён, semantic quality и production semantic readiness **не
подтверждены**. Для readiness нужен endpoint/model profile, возвращающий schema-valid
JSON в `content`/`parsed`, и повторный успешный matrix run.

## Review cycles

Cycle 1 нашёл MAJOR в schema-error gating, strict profile claims, repair payload,
staged missing-input interpretation и critic relation mutation. Все исправлены; затем
targeted и full suites прошли.

Cycle 2 нашёл MAJOR в relation semantics и user-declared role conflict. Все исправлены;
повторный full suite прошёл. Оставшийся MINOR — возможность mapper invent declaration —
также исправлен formal-identifier reconciliation и regression test.

## Final independent review

```text
FINAL REVIEW VERDICT: PASS

BLOCKER:
- None.

MAJOR:
- None.

MINOR:
- Документальные неточности в test counts и описании models.py исправлены до commit.

ACCEPTED_RISK:
- schema_enforced_by_server отражает принятый endpoint strict profile; обязательная
  recursive local validation остаётся fail-safe страховкой;
- possibly_attached_to допускается только как potential interface и блокирует completion;
- real endpoint reasoning-only, semantic readiness не установлена;
- prompt-only/non-strict допустимы только явно и отмечаются enforcement=false.

SCOPE VERDICT: PASS
```

Reviewer независимо подтвердил все acceptance gates:

- три отдельные schema-specific production stages и отдельные system/user roles;
- ATA-only `response_format`, корректные profiles/trace и отсутствие reasoning/embedded
  JSON fallback;
- Python candidate identity, exact critic coverage и immutable candidate anchors;
- safe document transition и MRO relation/location invariants;
- user-declared reconciliation;
- отсутствие повторных Go/No-Go facts extraction и technical retrieval;
- все completion/assessment/certificate gates;
- targeted/full/native HTTP/SSE/static checks;
- отсутствие новых dependencies и out-of-scope behavior;
- отсутствие unnecessary refactoring.

Scope review перечислил 13 изменённых файлов внутри allowlist, 0 вне allowlist,
0 unrelated behavior changes и 0 unnecessary refactoring.

## Accepted risks

- Server enforcement является свойством явно выбранного transport profile; локальная
  semantic/cross-reference validation остаётся обязательной.
- `possibly_attached_to` остаётся potential relation, но не даёт affected без critic и
  остальных gates.
- Текущий real endpoint несовместим с требованием `content`/`parsed`; semantic readiness
  не заявляется.
