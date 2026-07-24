# Ревью качества MRO ATA Impact MVP

## Вердикт

Текущий механизм пригоден только как консервативный прототип первого скрининга. Он не пригоден как основание для решения Go/No-Go, оценки объёма работ или утверждения применимости документации.

Его сильная сторона — объяснимый, быстрый и ограниченный список прямых кандидатов. Главный недостаток — система фактически классифицирует строки по ручным синонимам, а не понимает инженерный объект, повреждение, зону, применимость к типу ВС и различие между ATA системы, структурой и процедурой ремонта.

## Что реализовано сейчас

1. Из текста заявки выделяется явная ATA или совпадение с aliases в config/ata_catalog_overrides.json.
2. Все главы берутся из sertifikat_glavy_new.docx; наличие главы даёт только in_scope_candidate.
3. LLM может оставить либо отклонить только уже найденную правилами прямую ATA.
4. Простые impact_rules создают вторичные гипотезы.
5. В production-процессе document evidence отключён: MRO_KB_ATA_AGENT_EVIDENCE_ENABLED=0.

## Критические проблемы

### P0. Смешаны разные инженерные сущности

ATA главы используются одновременно как:

- система/оборудование, например ATA 34 для static port;
- структурная зона, например ATA 53/57;
- метод контроля или процедура, например NTM 51-10;
- признак полномочий из сертификата.

Эти сущности нельзя выдавать одним списком “затронутые ATA”. Для царапин около static port нужно отдельно оценивать:

- объект: static port — ATA 34;
- возможное повреждение окружающей обшивки — структурный scope ATA 53 только при наличии соответствующего факта;
- метод NDT/repair — документ, но не ATA scope;
- capability компании — отдельная проверка.

### P0. Ручные aliases не имеют достаточной доказательной базы

В текущем словаре восемь ATA глав и несколько общих слов на каждую. У записей есть source_id ata-ispec-2200, но нет редакции, раздела, применимости к семейству ВС, владельца термина, даты ревью и отрицательных условий.

Например, “шпангоут” ведёт к ATA 53, но наименование элемента само по себе не определяет структурную область, тип ремонта или применимость к конкретному aircraft family. Короткие stems для русских слов полезны для recall, но повышают риск ложных совпадений.

### P0. Сертификат проверяется слишком грубо

Наличие верхнеуровневой главы в DOCX не доказывает право выполнить конкретный ремонт. Не учитываются тип ВС, approval basis, допустимый вид работ, организационные процедуры, NDT capability, personnel authorization, ограничения OEM/DOA и наличие approved data.

Статус должен называться только “certificate chapter match”, а не capability candidate, пока не появится структурированная матрица полномочий.

### P1. Вторичные ATA не имеют инженерной модели связи

Impact rules вида “модификация → ATA 51” и “тяга → ATA 27” слишком широкие. Они создают ложные связи, потому что не различают:

- объект работы;
- затронутую систему;
- конструктивный интерфейс;
- метод проверки;
- действие в заявке.

### P1. LLM является недетерминированным фильтром без воспроизводимой оценки

LLM не может добавить ATA, что безопаснее генерации, но она может отбросить верный кандидат. Production-метрики LLM-контура не измерены отдельно от rules-only режима. Нет версии prompt/model response, схемы валидации результата и набора контрпримеров для отказа.

### P1. Документный поиск в прежнем виде небезопасен

При включении retrieval агент искал исторические MRO-документы и таблицы из data/output. Они содержат много сторонних ATA и procedural references. Это уже вызвало ложные ATA 25/27/29/51 для static port. Исторические кейсы могут быть полезны для аналогов и оценки трудоёмкости, но не как доказательство ATA scope.

## Проблемы тестовой оценки

- В наборе 71 запись, но только одна заявка с двумя разными ATA-главами. Multi-ATA качество не измерено.
- 68 описаний взяты из MRO-RAG problem_summary, а ожидаемые ATA — из связанной карточки. Runtime прямой карточки не читает, но набор всё же обогащён постфактум техническими деталями.
- Шесть описаний содержат SB/процедурные ссылки с номером главы. Их надо измерять отдельно как reference-present.
- expected_ata имеет статус cross_source_quality_candidate, а не экспертная разметка direct/secondary/procedure.

## Целевая архитектура

    intake text
      -> extraction of facts
      -> entity linking to MRO ontology
      -> direct ATA / structural ATA / secondary hypothesis / unknown
      -> certificate capability matrix
      -> controlled OEM document retrieval
      -> Go/No-Go evidence package

### 1. MRO ontology вместо flat aliases

Каждый термин должен быть отдельной версионированной сущностью:

| Поле | Пример |
| --- | --- |
| entity | static pressure port |
| entity_type | component |
| aircraft family | A320 family |
| ATA role | direct system ATA 34 |
| aliases | русский, английский, OEM naming |
| conflicts | skin damage is not automatically ATA 34 |
| source | OEM IPC/AMM или лицензированный ATA source, revision, section |
| owner/review | инженер-владелец, дата ревью |

Отдельные сущности нужны для дефектов, зон, structural features, действий, процедур и required input data.

### 2. Раздельный выход агента

Агент должен возвращать не единый список:

- direct_system_ata;
- direct_structural_ata;
- secondary_ata_hypotheses;
- procedure_references;
- required_input_data;
- certificate_chapter_match;
- decision: proceed_to_gonogo / request_information / decline.

### 3. Документы только по метаданным и роли

После direct ATA искать только контролируемо загруженные OEM AMM/SRM/IPC/CMM/ALS. Для каждого фрагмента обязательны:

- aircraft applicability;
- ATA/subchapter;
- document type;
- revision/effectivity;
- trust level;
- source path and exact section.

Исторические MRO-кейсы должны быть отдельным инструментом similar cases и никогда не должны подтверждать ATA или OEM procedure.

## Приоритетный план

1. Заморозить расширение flat aliases как основной стратегии; оставить их только как fallback и аварийно объяснимые правила.
2. Утвердить семантическую модель direct system / structural / secondary / procedure.
3. Создать экспертный gold set: не менее 100 raw intake заявок и 25+ multi-ATA заявок с разметкой ролей ATA.
4. Создать версионированную ontology table с provenance и review workflow.
5. Интегрировать controlled OEM documents с metadata filtering; historical cases оставить для similar-cases и стоимости.
6. Оценивать отдельно rules-only, ontology retrieval, LLM verifier и полный production pipeline.

## Критерии готовности к Go/No-Go

- ≥90% precision для direct ATA на expert-labelled raw intake set;
- ≥85% recall для direct ATA;
- отдельная multi-ATA recall ≥80% на 25+ заявках;
- 100% procedure references не попадают в ATA scope;
- 100% evidence имеет source/revision/applicability;
- сертификат сопоставляется с типом ВС и видом работы, а не только с номером главы.
