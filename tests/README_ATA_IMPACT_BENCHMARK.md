# Тестовая выборка ATA Impact Agent

`fixtures/ata_impact_from_mro_rag.jsonl` — консервативно отобранная подвыборка MRO-RAG: каждая запись одновременно присутствует в `com_offers/tests/ground truth.xlsx`, имеет связанный work order из реестра `case_registry.csv` и техническую ATA из карточки MRO-RAG.

- Вход агента: только `input.description` и необязательные поля типа ВС/MSN. Короткий заголовок из реестра заменяется на `problem_summary` связанной карточки только если тот содержит самодостаточные дефект, объект и техническую локализацию. Например, `TOP PANEL CORROSION` превращён в описание панели центрального топливного бака с Y/Rib/Stiff-координатами.
- Ожидаемые ATA: `expected_ata` из `ata_list` связанной карточки; технически общий код `00-00` исключён. Для первого слоя отдельно дано `expected_ata_chapters`.
- Документы исходного work order запрещено передавать на первом проходе: это предотвращает утечку правильного ответа.
- Метка `cross_source_quality_candidate` не заменяет экспертную разметку прямых и вторичных ATA, но исключает шаблонные ОПИ и записи без реального входящего запроса.

Пересборка требует все три источника:

```bash
python scripts/build_ata_impact_benchmark.py \
  --source /mnt/ii_models/Users/hizhenkov/MRO_RAG/apps/webapp/demo_data \
  --registry /mnt/ii_models/Users/hizhenkov/com_offers/pilot_artifacts/case_registry.csv \
  --ground-truth "/mnt/ii_models/Users/hizhenkov/com_offers/tests/ground truth.xlsx" \
  --output /tmp/ata_impact_raw.jsonl
python scripts/curate_quality_ata_descriptions.py \
  --input /tmp/ata_impact_raw.jsonl \
  --kept tests/fixtures/ata_impact_from_mro_rag.jsonl \
  --rejected tests/fixtures/ata_impact_rejected_after_quality_review.jsonl
python scripts/export_ata_impact_excel.py
```

Курация выполняется `scripts/curate_quality_ata_descriptions.py`. Она оставляет только записи, в которых одновременно есть дефект, конкретный объект и локализующий технический признак; исключает шаблонные документы, задания только по AD/AMOC, обрывки текста и дубликаты. Исключённые строки и причина остаются в `fixtures/ata_impact_rejected_after_quality_review.jsonl`; в основной benchmark они не попадают.

Для полноценной оценки агентского цикла эксперт должен заполнить `direct_ata`, `secondary_ata` и статус review у приоритетной подвыборки кейсов.
