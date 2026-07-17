# Логика поиска похожих коммерческих MRO-заявок

## Назначение

`mro-similar-cases` ищет аналоги новой коммерческой заявки среди исторических заявок из `com_offers`.
Это не расчет цены, не инженерное заключение и не финальное решение брать работу или не брать.
Модель должна быстро показать похожие заявки, объяснить причины похожести и отделить надежные источники от неподтвержденных документных связей.

Главный источник похожести:

- `com_offers/pilot_artifacts/case_registry.csv`
- `com_offers/Reestr_zayavok.xlsm`, sheet `Реестр`
- Markdown/OCR-презентации из `com_offers/converted_md` и `com_offers/converted_md_pdf_ocr`

Документы используются только как evidence layer:

- `com_offers/pilot_artifacts/case_document_links.csv`
- `com_offers/pilot_artifacts/case_documents.jsonl`
- Markdown/OCR-файлы из `normalized_md` или `converted_md_pdf_ocr`

## Поток обработки запроса

1. Пользователь выбирает модель `mro-similar-cases` в OpenWebUI и отправляет описание заявки.
2. API вызывает `CommercialOffersService.similar_cases()`.
3. Сервис использует исходный запрос без ручной лемматизации, падежных замен и query-specific aliases.
4. Запускается hybrid retrieval:
   - semantic search по заранее построенным embeddings карточек заявок;
   - lexical/BM25-подобный поиск по токенам;
   - exact matching по инженерным идентификаторам.
5. Кандидаты объединяются по `case_id`.
6. Финальный score складывается из нормализованных semantic, lexical и exact сигналов.
7. Для каждого кандидата строится структурный профиль заявки. Сейчас он используется как диагностический слой: добавляет `structured_score`, класс похожести и объяснения, но не дает отдельный boost в итоговый score.
8. Если включен внешний reranker, top candidates дополнительно ранжируются через `BAAI/bge-reranker-v2-m3` endpoint по компактному rerank-тексту, а не по длинному OCR.
9. Для top results подбираются документы evidence layer.
10. Ответ строится как таблица похожих заявок с предупреждениями по качеству источников и подсказками для будущего go/no-go/cost workflow.

## Query Rewrite

LLM query rewrite отключен по умолчанию и не является частью MVP retrieval.
Если включить его через `MRO_KB_QUERY_REWRITE_ENABLED=1`, он используется только как дополнительный источник вариантов запроса. Правильные ответы, падежные замены и соответствия `query -> case_id` в prompt или код добавлять нельзя.

## Converted Markdown Enrichment

Тяжелое чтение Markdown не выполняется при старте API. Отдельный шаг строит manifest из файлов:

- `com_offers/converted_md`
- `com_offers/converted_md_pdf_ocr`

Manifest хранится в `data_runtime/com_offers_converted_markdown_manifest.json`.
Связь с заявкой строится строго по номеру `MP/MRO/МР` в ближайшей case-папке или основном имени файла. Конфликтующие вложенные документы не индексируются молча, а попадают в report/warnings.

В поисковую карточку заявки добавляются:

- относительный путь к Markdown;
- заголовок презентации;
- первые содержательные строки;
- строки с инженерными идентификаторами и описанием запроса/проблемы.

Это дает data-driven сигналы без ручного `query -> case_id` хардкода. Например, если в реестре описание короткое, но в презентации есть `Dummy Gear`, `Zero stress jacking`, `Frame 35`, `technical ferry`, эти термины начинают участвовать в lexical/phrase scoring.

После обновления `converted_md*` нужно запускать `reindex-com-offers`, потому что enriched case-card text входит в embedding cache.

Текущий manifest строится без ручных aliases:

- manual `case_search_aliases.csv` не используется;
- ручная лемматизация/замены падежей не используются;
- вложенные example/archive/reference/tmp/прочность материалы пропускаются;
- конфликтующие case IDs в пути записываются в `conflicts` и не индексируются.

## `Reestr_zayavok.xlsm` Enrichment

`Reestr_zayavok.xlsm` используется как production-источник, потому что это реестр заявок, а не тестовая разметка.

Из листа `Реестр` в карточку заявки добавляются только поля, которые описывают работу:

- описание запроса;
- комментарии БД;
- задачи;
- заметки;
- имя папки.

Тип ВС из Excel не добавляется в similarity text и не используется как retrieval factor. Он остается display/check metadata из основного реестра.

После изменения или первого подключения `Reestr_zayavok.xlsm` нужно выполнить `reindex-com-offers`, потому что текст карточки заявки меняется и старые embeddings становятся stale.

## Retrieval Signals

### Semantic

Embeddings строятся заранее командой:

```bash
python3 -m apps.api.server reindex-com-offers
```

API при старте не пересчитывает embeddings. Если индекс отсутствует или устарел, `/api/health` должен показывать warning.

Semantic search работает по case-card text, собранному из полей реестра. Тип ВС из similarity text удаляется, чтобы `A320/A319/B737` не становились главным фактором похожести.

### Lexical

Lexical scoring использует токены из запроса и карточки заявки.
Токены не лемматизируются вручную. Это сделано намеренно: падежи, синонимы и русско-английские соответствия должны покрываться semantic embeddings, reranker и исходными данными, а не словарем в Python.

Сильнее учитываются совпадения в:

- `request_description`;
- точных фразах из описания;
- регистрационных/MSN данных, если они явно есть.

Шумные слова вроде `работы`, `возможность`, `временное`, `технический`, `изменение`, `анализ` не должны сами по себе поднимать нерелевантные заявки.

### Exact Matching

Exact matching применяется к инженерным идентификаторам:

- `ATA`
- `RIB`
- `FR` / `FRAME` / `ШП` / `шпангоут`
- `STGR` / `STRINGER` / `стрингер`
- `P/N` / `PN`
- `MSN`
- registration
- `AD` / `ДЛГ`
- `SB` / `СБ`
- `MP` / `MRO` case id

Тип ВС не входит в exact patterns.

### Aircraft Type

Тип ВС не является retrieval-сигналом.

Он используется только:

- как отображаемое поле в таблице;
- как слабый check, если в запросе явно указан тип ВС, а в найденной заявке указан другой;
- как минимальный tie-breaker после совпадения по проблеме, компоненту, зоне или идентификатору.

## Candidate Merge и Ranking

Кандидаты объединяются по `case_id`.

Для каждого кандидата сохраняются:

- `semantic_score`;
- `lexical_score`;
- `exact_score`;
- `structured_score`;
- `profile_semantic_score`;
- `rerank_score`;
- `similarity_reason_class`;
- `matched_queries`;
- `reasons`.

Итоговый score в текущей ветке не включает прямой structured-profile boost. Это осознанное ограничение: на текущем benchmark профильный boost ухудшал старую выдачу, поэтому профиль оставлен как explainability/diagnostic слой до улучшения candidate generation и качества профилей.

### Reranker

Reranker получает компактный текст заявки:

- `request_description`;
- `workscope_type`;
- `discipline_primary`;
- `bd_comments`;
- `certificate_scope_flag`;
- `status_normalized`;
- до 500 символов очищенного `Reestr_zayavok.xlsm`;
- до 700 символов очищенного converted Markdown/OCR;
- извлеченные exact identifiers.

Максимальная длина текста кандидата ограничена примерно 1500 символами. Это нужно, чтобы внешний `BAAI/bge-reranker-v2-m3` не зависал на длинных OCR-презентациях и оценивал именно коммерчески важную часть заявки.

## Evidence Layer

Evidence не участвует в similarity score.

Документ используется как надежный источник только если:

- `link_status == matched`;
- путь/`document_id` проходят проверку на номер заявки;
- файл читается;
- документ попадает в top evidence для данного запроса.

Статусы:

- `matched`: документ можно показывать как источник, если файл читается и связь совпадает с заявкой;
- `ambiguous_match`: документная связь требует проверки, документ не используется как надежный источник;
- `missing_document`: документов нет, похожесть рассчитана по реестру;
- `document_link_mismatch`: связанный документ не совпадает с номером заявки;
- `unreadable_document`: документ указан, но файл не удалось прочитать.

В top-level `sources` попадают только trusted documents с `source_type == commercial_offer_document`.

## Output Contract

Ответ должен содержать:

- таблицу похожих заявок;
- `case_id`;
- заказчика;
- тип ВС как display-only поле;
- статус;
- описание;
- почему заявка похожа;
- что отличается или что надо проверить;
- документы/Markdown, если есть надежная связь;
- предупреждения по качеству документов;
- явную фразу: `это поиск аналогов, не расчет цены и не финальное решение брать/не брать`.

## Метрики качества

Для ручного regression-набора считаются:

- `Hit@1`;
- `Hit@3`;
- `Hit@5`;
- `Hit@10`;
- `MRR`;
- `Precision@5`;
- `Recall@5`;
- `NDCG@5`.

Текущий целевой критерий MVP:

- `Hit@5 >= 0.80`;
- `Hit@10 >= 0.90`;
- правильный ответ не должен пропадать из расширенной выдачи.

Разметка используется только в тестах и не должна попадать в runtime-логику как соответствие `query -> case_id`.

Fallback lexical/exact без свежих embeddings ожидаемо слабее. Его задача - не заменить semantic search, а сохранить работоспособность и exact-identifier retrieval при stale/missing embedding index.

## MRO Case Profiles

MRO-профили - это структурный слой поверх текущего hybrid retrieval, `schema_version=3`.
Если полный профиль еще не построен LLM offline-процессом, runtime строит слабый fallback-профиль из production-текста заявки: точные identifiers, ATA и зоны извлекаются regex-паттернами, а компоненты/дефекты не подставляются словарями.

Идея:

- offline строить компактный JSON-профиль каждой заявки из production-источников;
- online строить слабый технический профиль пользовательского запроса без выбора `case_id`;
- сравнивать одинаковые структурные поля, а не длинный сырой OCR;
- использовать профиль для объяснения, оценки пригодности аналога и будущего отбора candidate pool;
- оставить semantic/lexical/exact retrieval как основной ranking path до достижения MVP-метрик.

Профиль не должен содержать правильный `case_id` для запроса, ручные aliases или исправления под benchmark. Он должен извлекать только наблюдаемые признаки: дефект, компонент, зону, действие, ограничения, идентификаторы и двуязычные поисковые термины.

Минимальные поля профиля:

- `problem_summary`;
- `work_type`;
- `defect_type`;
- `aircraft_type_metadata`;
- `ata`;
- `components`;
- `zones`;
- `identifiers`;
- `authority_path`;
- `action_required`;
- `constraints_or_risks`;
- `search_terms_ru_en`;
- `evidence_fields`;
- `confidence`.

Тип ВС в профиле допускается как отдельное metadata/check поле. Он сохраняется в `metadata_text`, но отделяется от основного profile `search_text`, чтобы быть доступным для проверки и tie-breaker, не становясь самостоятельным главным retrieval-сигналом.

Offline LLM-профиль записывается в cache только если проходит quality gate. Профили с `...`, низкой уверенностью, path/OCR-noise или недостаточным структурным сигналом попадают в failures и не индексируются.

Текущий fallback-профиль намеренно консервативен:

- извлекает ATA, зоны и точные идентификаторы;
- не угадывает broad-сущности вроде `landing gear`, `corrosion`, `repair` по ручным словарям;
- ставит низкую `confidence`, чтобы fallback не выглядел как полноценный инженерный профиль.

`structured_score` считается по пересечению identifiers, authority path, components, zones, ATA, defect type и work type. В текущей ветке он не прибавляется к ranking score, но используется для объяснения и классификации аналога.

Финальная выдача дополнительно содержит:

- `structured_score`;
- `similarity_reason_class`: `same_identifier`, `same_component_defect_zone`, `same_component_defect`, `same_work_type`, `commercially_similar`, `weak_analog`;
- `go_no_go`: подсказки по рискам и недостающим данным, не финальное решение;
- `cost_readiness`: пригодность найденного аналога как comparable для будущей оценки стоимости, не расчет цены.
