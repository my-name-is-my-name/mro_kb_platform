# ATA Impact Agent

`mro-ata-impact` — самостоятельный первый слой контура Go/No-Go. Он разделяет
техническую ATA classification, проверку certificate scope и документально
подтверждённое воздействие. Эти результаты не являются capability approval.

## Pipeline v2

```text
formal identifiers
→ LLM engineering facts (без ATA)
→ LLM ATA mapping (без legacy allowlist)
→ deterministic certificate validation
→ independent critic
→ OEM evidence retriever
→ deterministic status assembly
```

`standard` выполняет два LLM-вызова: facts и объединённый runtime-вызов
mapping+critic при логически раздельных стадиях. `extended` выполняет три.
`auto` выбирает extended для AD/SB, нескольких объектов/отношений, модификаций
и сложных неопределённостей.

Результат разделяет `affected_ata`, `potentially_affected_ata` и `context_ata`.
Location reference никогда автоматически не становится affected. Interface
гипотеза обязана ссылаться на relation ID. Procedure ATA не подтверждается без
применимого controlled OEM документа.

## OEM corpus

Точка подключения — `core.ata_impact.evidence.AtaEvidenceRetriever.search()`.
При отсутствии корпуса `NullAtaEvidenceRetriever` возвращает `not_configured`.
Будущий ingest сохраняет оригинальный PDF, normalized Markdown, structured JSON
metadata (revision/effectivity/applicability/section), chunks, lexical
SQLite FTS/BM25 index, vector Qdrant/Chroma index и reranker. Vector DB —
восстанавливаемый индекс, а не источник истины.

Адаптер возвращает controlled OEM/approved data с `document_id`,
`document_type`, `revision`, `effectivity`, `section_reference`, `trust_level`,
`applicable` и точные `confirmed_candidates` с category/entity/relation. Исторические кейсы и интернет не дают
`document_confirmed`.

## Контракт

`POST /api/ata-impact` принимает `request` и, при наличии, `aircraft_type`, `component`/`components`, `zone`/`zones`, `part_number`, `ata` либо объект `fields` с теми же полями. OpenAI-совместимый вызов: `POST /v1/chat/completions`, модель `mro-ata-impact`.

Ответ v1 разделяет:

- `direct_system_ata` — система или компонент, прямо найденные в заявке;
- `direct_structural_ata` — конструкция, только если текст подтверждает её повреждение;
- `secondary_ata_hypotheses` — связи для инженерной проверки;
- `procedure_references` — AMM/SRM/NTM/IPC/CMM/ALS; они никогда не создают ATA scope;
- `required_input_data` и `decision`: `proceed_to_go_no_go` или `request_information`;
- `certificate_chapter_match` — только совпадение главы сертификата, не capability.

Production mode HTTP API — `auto`; также доступны `standard` и `extended`.
`rules_only`, `ontology_llm` и `full_pipeline` сохранены как deprecated explicit
legacy fallback и не используются по умолчанию.

В v2 LLM самостоятельно создаёт кандидатов; certificate catalog используется
только после mapping. Legacy ontology не ограничивает список. Только applicable
controlled OEM/approved data может дать `document_confirmed`.

В OpenWebUI этапы передаются в сворачиваемом `reasoning`-блоке. Это журнал действий инструмента, а не скрытые рассуждения модели.

## Источники и границы

Онтология находится в `config/mro_ontology_v1.json`, имеет версию, источник, владельца, дату ревью и условия применимости каждой связи. Рабочая применимость на уровне subchapter, процедуры или ограничения подтверждается только контролируемо загруженными OEM AMM/SRM/IPC/CMM/ALS.

Публичные EASA/FAA материалы допускаются как дополнительный контекст. Нормативная схема ATA должна загружаться из лицензированной ATA iSpec 2200, когда она будет предоставлена владельцем лицензии.

## Проверка

`python -m unittest tests.test_ata_impact_agent` — unit-тесты агента.

`python3.13 scripts/evaluate_ata_impact_benchmark.py --mode legacy-rules` —
deprecated offline baseline. Для v2 semantic benchmark:

```bash
MRO_KB_ATA_AGENT_LLM_ENABLED=1 \
python3.13 scripts/evaluate_ata_impact_benchmark.py --mode v2-llm
```

`--mode v2-fallback` отдельно проверяет safe explicit-only fallback и не является
измерением semantic quality.
