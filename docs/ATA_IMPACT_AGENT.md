# ATA Impact Agent

`mro-ata-impact` — самостоятельный первый слой контура Go/No-Go. Он разделяет
техническую ATA classification, проверку certificate scope и документально
подтверждённое воздействие. Эти результаты не являются capability approval.

## Pipeline v2

```text
formal identifiers
→ LLM engineering facts (без ATA)
→ LLM ATA mapping (без legacy allowlist)
→ deterministic mapping validation
→ deterministic certificate validation
→ independent critic
→ OEM evidence retriever
→ deterministic status assembly
```

Все production modes выполняют минимум три независимых LLM-вызова: facts,
mapping и critic. Critic получает уже провалидированные mapping candidates с
уникальными `candidate_id` и результат certificate validation. `standard`,
`extended` и `auto` могут отличаться глубиной critic и retrieval, но не
объединяют mapper и critic в одном контексте.

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
- `certificate_chapter_match` — только совпадение главы сертификата, не capability.

Production mode HTTP API — `auto`; также доступны `standard` и `extended`.
`rules_only`, `ontology_llm` и `full_pipeline` сохранены как deprecated explicit
legacy fallback и не используются по умолчанию.
Legacy mode разрешён только при точном имени mode и явном feature flag
`MRO_KB_ENABLE_LEGACY_ATA_MODES=true`; неизвестный mode отклоняется.

В v2 LLM самостоятельно создаёт кандидатов; certificate catalog используется
только после mapping. Legacy ontology не ограничивает список. Только applicable
controlled OEM/approved data может дать `document_confirmed`.

В OpenWebUI этапы передаются в сворачиваемом `reasoning`-блоке. Это журнал действий инструмента, а не скрытые рассуждения модели.

## Источники и границы

Онтология находится в `config/mro_ontology_v1.json`, имеет версию, источник, владельца, дату ревью и условия применимости каждой связи. Рабочая применимость на уровне subchapter, процедуры или ограничения подтверждается только контролируемо загруженными OEM AMM/SRM/IPC/CMM/ALS.

Публичные EASA/FAA материалы допускаются как дополнительный контекст. Нормативная схема ATA должна загружаться из лицензированной ATA iSpec 2200, когда она будет предоставлена владельцем лицензии.

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
