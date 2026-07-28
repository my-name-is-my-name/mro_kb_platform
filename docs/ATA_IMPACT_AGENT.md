# ATA Impact Agent

`mro-ata-impact` — самостоятельный первый слой контура Go/No-Go. Он разделяет
техническую ATA classification, проверку certificate scope и документально
подтверждённое воздействие. Эти результаты не являются capability approval.

## Pipeline v2

```text
formal identifiers
→ structured LLM engineering facts (без ATA)
→ ГОСТ 18675-2012 civil classification reference retrieval
→ structured LLM ATA mapping (без legacy allowlist и candidate_id)
→ deterministic mapping validation
→ Python candidate_id allocation
→ deterministic certificate validation
→ independent structured LLM critic
→ OEM evidence retriever
→ deterministic status assembly
```

Все production modes выполняют минимум три независимых LLM-вызова: facts,
mapping и critic. Critic получает уже провалидированные mapping candidates с
уникальными `candidate_id` и результат certificate validation. `standard`,
`extended` и `auto` могут отличаться глубиной critic и retrieval, но не
объединяют mapper и critic в одном контексте.

ГОСТ-справочник и сертификат имеют разные роли. Приложение А ГОСТ
18675-2012 индексируется отдельно по главам ATA; mapper получает не весь
документ, а максимум восемь коротких релевантных карточек.
`sertifikat_glavy_new.docx` проверяет область
сертификата только после mapping и не может подавлять технически правильную
ATA. Справочник ГОСТ не является OEM evidence и не подтверждает процедуры.

При `MRO_KB_ATA_REFERENCE_VECTORS_ENABLED=true` используется локальный
кешированный индекс embeddings (`MRO_KB_EMBEDDING_MODEL`, по умолчанию
`bge-m3:latest`) и один query-embedding на заявку. Индекс не перестраивается
при каждом запросе. Если индекс или embedding endpoint недоступен, retrieval
явно переходит в `lexical_fallback`; локальная schema/MRO validation остаётся
обязательной.

Для stages `engineering_fact_extraction`, `ata_mapping`,
`independent_critic` и `json_repair` ATA-specific transport передаёт
generation schema через OpenAI-compatible `response_format`. Профиль
`qwen_completion` используется по умолчанию для текущего Qwen endpoint и
сразу закрывает thinking-блок, не выполняя скрытых capability generations.
Явный профиль `auto` может последовательно проверить несколько transport modes,
кеширует выбранный профиль и предназначен только для диагностики неизвестного
endpoint. `json_schema` использует strict schema
adapter; `json_schema_no_strict`,
`json_object` и `prompt_only` явно отражаются в trace без ложного признака
server enforcement. Любой результат повторно проходит полную локальную
shape-validation и MRO cross-reference validation. Пустой `content`,
`finish_reason=length`, reasoning-only ответ и произвольный embedded JSON
завершаются repair/fail-safe, не извлечением chain-of-thought.

Mapper возвращает только инженерные поля. После deterministic validation
Python назначает уникальный request-scoped ID формата
`candidate:<category>:<anchor>:<ata-token>:<sequence>`. Critic может ссылаться
на него, но не менять ATA, category или identity anchor. Relation, нужная
только для downgrade transition, хранится отдельно как
`transition_relation_id`.

Результат разделяет `affected_ata`, `potentially_affected_ata` и `context_ata`.
Location reference никогда автоматически не становится affected. Interface
гипотеза обязана ссылаться на relation ID. Кандидат без ровно одного critic
verdict остаётся `candidate_unverified`. `require_document` переводит кандидата
в `document_verification_required`, а не в affected. Procedure ATA не
подтверждается без применимого controlled OEM документа.

## OEM corpus

Точка подключения — `core.ata_impact.evidence.AtaEvidenceRetriever.search()`.
При отсутствии корпуса `NullAtaEvidenceRetriever` возвращает `not_configured`.
Retriever получает не только список ATA, но и точные validated
`mapping_candidates` с request-scoped `candidate_id`, category и anchors.
Controlled verifier обязан вернуть подтверждение по этим ID; поиск только по
ATA не может выполнить state transition.
Будущий ingest сохраняет оригинальный PDF, normalized Markdown, structured JSON
metadata (revision/effectivity/applicability/section), chunks, lexical
SQLite FTS/BM25 index, vector Qdrant/Chroma index и reranker. Vector DB —
восстанавливаемый индекс, а не источник истины.

Адаптер возвращает controlled OEM/approved data с `document_id`,
`document_type`, `revision`, `effectivity`, `section_reference`, `trust_level`,
`applicable`, `current_revision`, `verification_status=confirmed` и точные
`confirmed_candidates` с `candidate_id`, ATA, category, entity/relation anchor,
record-level `verification_status=confirmed` и непустым `confirmed_claim`.
Исторические кейсы и интернет не дают `document_confirmed`.

## Контракт

`POST /api/ata-impact` принимает `request` и, при наличии, `aircraft_type`, `component`/`components`, `zone`/`zones`, `part_number`, `ata` либо объект `fields` с теми же полями. OpenAI-совместимый вызов: `POST /v1/chat/completions`, модель `mro-ata-impact`.

Compatibility-поля v1 разделяют:

- `direct_system_ata` — система или компонент, прямо найденные в заявке;
- `direct_structural_ata` — конструкция, только если текст подтверждает её повреждение;
- `secondary_ata_hypotheses` — связи для инженерной проверки;
- `procedure_references` — AMM/SRM/NTM/IPC/CMM/ALS; они никогда не создают ATA scope;
- `required_input_data`; старые ATA buckets сохраняются как проекция v2 state,
  но authoritative `decision` использует v2 states `completed`,
  `completed_with_hypotheses`, `engineering_review_required`,
  `additional_input_required` или `document_verification_required`;
- `certificate_assessment` — основной business gate по
  `sertifikat_glavy_new.docx`: `covered`, `partially_covered`, `not_covered`,
  `undetermined` или `catalog_unavailable`;
- `certificate_chapter_match` — deprecated compatibility projection, не
  окончательное capability approval.

Запись сертификата на уровне главы покрывает её подглавы: например, кандидат
`ATA 53-10` сопоставляется со строкой `ATA 53` с `match_type=chapter`.
`match_type=exact` используется только для точной строки. Context-only ATA не
участвует в итоговой оценке сертификата.

User-declared ATA детерминированно reconciles после critic и certificate
validation: `user_declared_consistent` не блокирует закрытый анализ;
`user_declared_conflicting`, `user_declared_unverified` и
`user_declared_not_in_certificate` требуют review. Mapper assessment остаётся
неавторитетным полем `declared_assessment`.

Production mode HTTP API — `auto`; также доступны `standard` и `extended`.
`rules_only`, `ontology_llm` и `full_pipeline` сохранены как deprecated explicit
legacy fallback и не используются по умолчанию.
Legacy mode разрешён только при точном имени mode и явном feature flag
`MRO_KB_ENABLE_LEGACY_ATA_MODES=true`; неизвестный mode отклоняется.

В v2 LLM самостоятельно создаёт кандидатов; certificate catalog используется
только после mapping. Legacy ontology не ограничивает список. Только applicable
controlled OEM/approved data может дать `document_confirmed`.

Go/No-Go использует готовые staged `engineering_facts`,
`document_verification`, `retrieved_documents` и `controlled_evidence`. Он не
запускает второй technical fact extractor и не повторяет technical evidence
retrieval. Authoritative `validation_gate` формируется ATA service; flat intake
fields сохраняются только как compatibility projection.

В OpenWebUI этапы передаются в сворачиваемом `reasoning`-блоке. Это журнал действий инструмента, а не скрытые рассуждения модели.

## Источники и границы

Первичная система нумерации v2 загружается из нормализованного приложения А
ГОСТ 18675-2012. Источник используется как классификационный справочник, но не
как OEM evidence и не как подтверждение capability. SHA-256 предоставленного
исходного PDF хранится в метаданных справочника; сам PDF не входит в Git.

АП-25 описывает требования лётной годности и не используется для определения
ATA. Рабочая применимость процедуры или ремонта подтверждается только
контролируемо загруженными OEM AMM/SRM/IPC/CMM/ALS. Для иностранного ВС
классификация по ГОСТ остаётся предварительной до появления применимой
OEM-документации.

## Проверка

`python -m unittest tests.test_ata_impact_agent` — unit-тесты агента.

`MRO_KB_ENABLE_LEGACY_ATA_MODES=true python3.13 scripts/evaluate_ata_impact_benchmark.py --mode legacy-rules` —
deprecated offline baseline. Для v2 semantic benchmark:

```bash
MRO_KB_ATA_AGENT_LLM_ENABLED=1 \
python3.13 scripts/evaluate_ata_impact_benchmark.py --mode v2-llm
```

`--mode v2-fallback` отдельно проверяет safe explicit-only fallback и не является
измерением semantic quality.
