# Техническое задание на развёртывание T-Search

## Назначение

Развернуть локальный T-Search как отдельный сервис многошагового поиска доказательств для MVP go/no-go. T-Search ищет и ранжирует фрагменты документов, но не принимает итоговое решение о возможности выполнения заявки.

Сценарии:

- поиск AMM, CMM, SRM, IPC, WDM, ALS, AD, SB, AMOC и внутренних инженерных документов;
- поиск по косвенным признакам и нескольким ATA;
- выявление подтверждённых и ещё не покрытых частей вопроса;
- подготовка компактного evidence set для экспертного анализа.

## Развёртывание

Модель находится в `/mnt/ii_models/T-Search`. Это agentic retriever на базе Qwen3.6-35B-A3B. По инструкции модели требуется официальный T-Search harness с инструментами `search_corpus`, `save_and_advance` и `finalize_ranking`.

Модель запускается отдельным сервисом. `mro_kb_platform` не должен загружать модель в свой процесс. Пример serving-конфигурации из README модели:

```bash
SGLANG_ENABLE_SPEC_V2=1 \
python3 -m sglang.launch_server \
  --model-path /mnt/ii_models/T-Search \
  --served-model-name t-tech/T-Search \
  --tp 2 --host 0.0.0.0 --port 8000 \
  --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
  --context-length 65536 --chunked-prefill-size 16384
```

Фактические GPU, tensor parallelism, memory fraction и latency должны быть подтверждены нагрузочным тестом. Модель не должна быть доступна напрямую из внешней сети.

## Corpus search backend

T-Search получает документы только через search backend:

```http
POST /search
Content-Type: application/json
```

```json
{
  "query": "повреждение FR 35 ремонт и влияние на соседние системы",
  "filters": {
    "aircraft_type": "A320",
    "ata": ["ATA 53", "ATA 51"],
    "document_types": ["AMM", "SRM", "ALS", "AD", "SB"]
  },
  "limit": 12
}
```

Ответ каждого результата должен содержать `chunk_id`, `document_id`, `title`, `text`, `source_path`, `page` или `section`, `document_type`, `revision`, `aircraft_type`, `ata` и `score`.

Backend объединяет exact, lexical/BM25, semantic и optional reranking search и применяет metadata filters. На первом этапе разрешается использовать текущие SQLite chunks. Длинные OCR-файлы целиком в контекст не передаются.

## Adapter в MRO KB Platform

Использовать отдельный адаптер с настройками:

```text
MRO_KB_TSEARCH_ENABLED=0|1
MRO_KB_TSEARCH_URL=http://host:port
MRO_KB_TSEARCH_TIMEOUT_SECONDS=60
MRO_KB_TSEARCH_MAX_ROUNDS=3
MRO_KB_TSEARCH_LIMIT=8
```

Адаптер возвращает `status`, `documents`, `warnings` и `search_trace`. При disabled, timeout или ошибке система обязана перейти к текущему SQLite/hybrid retrieval и добавить `tsearch_fallback_used`. Недоступность T-Search не является основанием для `NO_GO`.

T-Search вызывается после извлечения фактов и расширения области воздействия. В запрос передаются исходное описание, тип ВС, direct/potential ATA, компонент, зона, work type и открытые вопросы. T-Search возвращает evidence, но не recommendation.

## Доказательность и безопасность

Для каждого результата сохраняются поисковый запрос, фильтры, документ, страница/раздел, ревизия, версия индекса, число раундов и причины сохранения фрагмента.

Внешний web search является отдельным инструментом с allowlist официальных доменов. Найденная веб-страница не считается автоматически достаточным основанием. Неизвестная применимость или конфликт ревизий переводят заявку в `HOLD_EXPERT_REVIEW`.

## Приёмка

- health-check модели, harness и search backend;
- выдача ранжированных chunks с реквизитами;
- работа фильтров по ATA, типу документа и типу ВС;
- корректный fallback при timeout/ошибке;
- T-Search не изменяет recommendation напрямую;
- shadow-сравнение с текущим hybrid retrieval на 30–50 экспертных заявках;
- измерение Recall@10, precision применимых документов, доли ложных evidence, latency и качества fallback;
- включение по умолчанию только после подтверждения, что не выросла доля ложных `GO`.
