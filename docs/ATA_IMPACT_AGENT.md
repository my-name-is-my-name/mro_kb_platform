# ATA Impact Agent

`mro-ata-impact` — самостоятельный первый слой контура Go/No-Go. Он определяет ATA scope по контролируемой онтологии и наличие главы в сертификате, но не утверждает ремонт, изменение или возможность выполнения работ.

## Контракт

`POST /api/ata-impact` принимает `request` и, при наличии, `aircraft_type`, `component`/`components`, `zone`/`zones`, `part_number`, `ata` либо объект `fields` с теми же полями. OpenAI-совместимый вызов: `POST /v1/chat/completions`, модель `mro-ata-impact`.

Ответ v1 разделяет:

- `direct_system_ata` — система или компонент, прямо найденные в заявке;
- `direct_structural_ata` — конструкция, только если текст подтверждает её повреждение;
- `secondary_ata_hypotheses` — связи для инженерной проверки;
- `procedure_references` — AMM/SRM/NTM/IPC/CMM/ALS; они никогда не создают ATA scope;
- `required_input_data` и `decision`: `proceed_to_go_no_go` или `request_information`;
- `certificate_chapter_match` — только совпадение главы сертификата, не capability.

Параметр `mode` принимает `rules_only`, `ontology_llm` и `full_pipeline`; production-значение по умолчанию для HTTP API — `full_pipeline`. В `full_pipeline` агент выполняет не более двух проходов: сначала ищет гипотезы только в controlled OEM/approved базе, затем (только при нехватке данных) делает один внешний поиск как контекст.

LLM может только отфильтровать кандидатов, уже разрешённых онтологией; она не создаёт ATA, не меняет ATA-роль и не подтверждает capability. Secondary ATA получает статус `confirmed_affected` только при applicable карточке `controlled_oem` или `approved_data`. EASA/FAA внешние ссылки получают `regulatory_external` и могут объяснять обязательность проверки, но не заменяют OEM-процедру; прочие внешние материалы — `internet_unverified`. Historical cases и internal reference не подтверждают ATA scope.

В OpenWebUI этапы передаются в сворачиваемом `reasoning`-блоке. Это журнал действий инструмента, а не скрытые рассуждения модели.

## Источники и границы

Онтология находится в `config/mro_ontology_v1.json`, имеет версию, источник, владельца, дату ревью и условия применимости каждой связи. Рабочая применимость на уровне subchapter, процедуры или ограничения подтверждается только контролируемо загруженными OEM AMM/SRM/IPC/CMM/ALS.

Публичные EASA/FAA материалы допускаются как дополнительный контекст. Нормативная схема ATA должна загружаться из лицензированной ATA iSpec 2200, когда она будет предоставлена владельцем лицензии.

## Проверка

`python -m unittest tests.test_ata_impact_agent` — unit-тесты агента.

`python scripts/evaluate_ata_impact_benchmark.py` — офлайн-оценка по обезличенной выборке из MRO-RAG. Скрипт измеряет только recall главы ATA; выборка не передаёт исходные документы агенту, чтобы исключить утечку ответа.
