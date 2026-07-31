# MRO Services Stable State

Дата фиксации: 2026-07-31  
Хост: `stressii2.trafic.rujv`  
Основной сетевой адрес для тестов: `10.100.112.51`

## Цель документа

Этот отчёт фиксирует сегодняшнее стабильное состояние частей проекта, связанных с оценкой MRO-заявок, поиском похожих случаев, ATA Impact, PDF-to-Markdown conversion и новым `mro-docs-kb`.

Документ нужен, чтобы можно было:

- понять, какие сервисы запущены и на каких портах;
- восстановить проверенную конфигурацию;
- откатиться на конкретные commit SHA;
- отличить стабильные части от экспериментальных/неполных.

## Репозитории и стабильные commits

### `mro_kb_platform`

Путь:

```text
/mnt/ii_models/Users/hizhenkov/mro_kb_platform
```

Remote:

```text
git@github.com:my-name-is-my-name/mro_kb_platform.git
```

Branch:

```text
platform/experiment
```

Стабильный commit на 2026-07-31:

```text
7995331 Disable ATA impact on main API port
```

Назначение текущего состояния:

- порт `8121` оставлен для `mro-kb`, `mro-similar-cases`, `mro-go-no-go`;
- `mro-ata-impact` намеренно убран с `8121`;
- попытка вызвать `mro-ata-impact` через `8121` возвращает `404`;
- legacy `/api/ata-impact` на `8121` возвращает `410 Gone` с указанием использовать `http://10.100.112.51:8122`.

Последние commits:

```text
7995331 Disable ATA impact on main API port
5129805 Simplify ATA impact grounding and fallback
f998218 Simplify ATA structured output pipeline
c8c77d9 Harden ATA Impact v2 safety pipeline
023e61d Add staged LLM-first ATA impact pipeline
```

### `mro_docs_kb`

Путь:

```text
/mnt/ii_models/Users/hizhenkov/mro_docs_kb
```

Remote:

```text
git@github.com:my-name-is-my-name/mro_docs_kb.git
```

Branch:

```text
experiment/mro-similar-cases-structured-profiles
```

Стабильный commit на 2026-07-31:

```text
b471abf Add full converted MPD manifest ingestion
```

Что сделано:

- создан отдельный runtime `mro-docs-kb`;
- добавлен импорт полного batch manifest от `convert_md`;
- импортирован полный `A320 FAMILY MPD Revision 50`;
- добавлен endpoint `/api/kb/admin/ingest-converted-manifest`;
- исправлена JSON-сериализация metadata в admin ingest responses;
- добавлена нормализация русских поисковых запросов, например `закрылков -> flap`;
- добавлен режим ответа для запросов вида `перечисли таски ...`;
- добавлены unit tests для full manifest ingestion, page offsets и русского запроса `перечисли таски для закрылков`.

Текущее runtime-состояние БД:

```text
storage: sqlite_test_adapter
source_files: 2
documents: 1
document_revisions: 1
pages: 2182
chunks: 5836
```

Загруженный документ:

```text
A320 FAMILY MPD
Revision 50
Revision date: 2025-11-01
source_contour: operational_docs
current_revision: null
```

Важное ограничение:

- PostgreSQL ещё не включён как runtime storage;
- текущий storage всё ещё SQLite adapter;
- полный MPD импортирован в SQLite runtime DB;
- Qdrant collection доступна, но полная переиндексация всех `5836` chunks в Qdrant ещё не зафиксирована как завершённая операция;
- exact/lexical поиск по полному MPD работает;
- vector retrieval технически включён, но его качество до полного reindex нельзя считать финальным.

Последние commits:

```text
b471abf Add full converted MPD manifest ingestion
8813b5a Add converted MPD chunk ingestion
cdd9dfa Add A320 MPD source config
dc0ff3c Add Mineru extraction and external discovery adapters
f667b28 Add standalone mro docs knowledge base
```

### `convert_md`

Путь:

```text
/mnt/ii_models/Users/hizhenkov/convert
```

Remote:

```text
git@github.com:my-name-is-my-name/convert_md.git
```

Branch:

```text
main
```

Стабильный commit на 2026-07-31:

```text
27b41bf Add resumable batch PDF conversion
```

Что сделано:

- создан отдельный Docker-based PDF-to-Markdown service;
- исходные PDF не копируются в repo;
- Mineru image `mineru:latest` остаётся внешней Docker-зависимостью;
- добавлен resumable batch converter:

```text
scripts/convert_pdf_batches.py
```

- полный `MPDA320V1_R50_I00.pdf` сконвертирован батчами по 50 страниц.

Результат полной конвертации:

```text
/mnt/ii_models/Users/hizhenkov/convert/data/output/MPDA320V1_R50_I00/_full_markdown/MPDA320V1_R50_I00.full.md
```

Manifest:

```text
/mnt/ii_models/Users/hizhenkov/convert/data/output/MPDA320V1_R50_I00/_full_markdown/manifest.json
```

Проверенные параметры результата:

```text
PDF pages: 2182
completed_batches: 44
failed_batches: 0
Markdown size: 8565833 bytes
manifest size: 101134 bytes
directory permissions: 777
file permissions: 666
```

Последние commits:

```text
27b41bf Add resumable batch PDF conversion
5180483 Add PDF to Markdown conversion service
```

## Запущенные сервисы и порты

### OpenWebUI / Intranet RAG infrastructure

| Порт | Сервис | Runtime | Состояние | Назначение |
|---:|---|---|---|---|
| `3000` | `intranet-rag-open-webui-1` | Docker | `Up`, healthy | OpenWebUI UI |
| `9099` | `intranet-rag-pipelines-1` | Docker | `Up` | OpenWebUI pipelines |
| `6333` | `intranet-rag-qdrant-1` | Docker | `Up` | Qdrant HTTP API |
| `11434` | `intranet-rag-ollama-1` | Docker | `Up` | Ollama embeddings, `bge-m3:latest` |

### MRO platform services

| Порт | Сервис | Unit/container | Команда | Назначение |
|---:|---|---|---|---|
| `8121` | MRO KB Platform API | `mro-kb-api.service` | `python3 -m apps.api.server --host 0.0.0.0 --port 8121` | `mro-kb`, `mro-similar-cases`, `mro-go-no-go` |
| `8122` | ATA Impact API | `mro-ata-impact.service` | `python3 -m apps.api.ata_server --host 10.100.112.51 --port 8122` | правильный isolated `mro-ata-impact` |
| `8131` | Docs KB API | `mro-docs-kb-test.service` | `python3 -m apps.api.server --host 0.0.0.0 --port 8131` | отдельный `mro-docs-kb` для OpenWebUI |
| `8095` | Convert API | Docker `convert-api` | `uvicorn convert_api.main:app --host 0.0.0.0 --port 8095` | PDF-to-Markdown conversion |

Важно по `8131`:

- `mro-docs-kb-test.service` сейчас запущен как transient user systemd unit через `systemd-run --user`;
- после перезагрузки или остановки user session он может не подняться автоматически;
- для production нужно оформить постоянный unit file по аналогии с `mro-kb-api.service`.

## OpenWebUI endpoints

### `mro-kb`, `mro-similar-cases`, `mro-go-no-go`

Base URL:

```text
http://10.100.112.51:8121/v1
```

Models на `8121`:

```text
mro-kb
mro-similar-cases
mro-go-no-go
```

Проверка:

```bash
curl http://127.0.0.1:8121/v1/models
```

Контрольное поведение:

```text
mro-ata-impact на 8121 не обслуживается.
```

Проверка:

```bash
curl -X POST http://127.0.0.1:8121/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mro-ata-impact","messages":[{"role":"user","content":"test"}],"stream":false}'
```

Ожидаемо:

```text
404 Unknown model: mro-ata-impact
```

Legacy endpoint:

```bash
curl -X POST http://127.0.0.1:8121/api/ata-impact \
  -H 'Content-Type: application/json' \
  -d '{"request":"test"}'
```

Ожидаемо:

```text
410 Gone
mro-ata-impact is not served on port 8121; use http://10.100.112.51:8122
```

### `mro-ata-impact`

Base URL:

```text
http://10.100.112.51:8122/v1
```

Model:

```text
mro-ata-impact
```

Проверка:

```bash
curl http://10.100.112.51:8122/v1/models
```

Smoke request:

```bash
curl -X POST http://10.100.112.51:8122/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mro-ata-impact","messages":[{"role":"user","content":"test ATA 27 flaps"}],"stream":false}'
```

Проверенное поведение на 2026-07-31:

```text
service responds as mro-ata-impact
contract_version: v2
runtime_mode: extended
engineering_review_required
```

### `mro-docs-kb`

Base URL:

```text
http://10.100.112.51:8131/v1
```

Model:

```text
mro-docs-kb
```

Проверка:

```bash
curl http://10.100.112.51:8131/v1/models
curl http://10.100.112.51:8131/api/health
```

Search API:

```bash
curl -X POST http://10.100.112.51:8131/api/kb/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"A320 FAMILY MPD REVISION 50 NOV 01/25","limit":3}'
```

OpenWebUI smoke prompt:

```text
перечисли таски для закрылков
```

Проверенное поведение:

```text
Ответ возвращает MPD task candidates по A320 FAMILY MPD Revision 50,
например section 27-44-51 / ATA 27 / page 71.
```

Пример формата ответа:

```text
Найдены MPD task candidates по локальному A320 FAMILY MPD:
- 574605-01-1 - 27-44-51 / ATA 27 / page 71
- 575101-01-1 - 27-44-51 / ATA 27 / page 71
- 575151-02-1 - 27-44-51 / ATA 27 / page 71
...
```

Ограничение:

```text
Это candidates из найденных chunks, а не окончательно нормализованная таблица MPD tasks.
Нужен отдельный table parser/reranker для строгого извлечения task rows.
```

### `convert-api`

Base URL:

```text
http://10.100.112.51:8095
```

Health:

```bash
curl http://127.0.0.1:8095/health
```

Проверенное состояние:

```text
status: ok
mineru_image: mineru:latest
mineru_image_available: true
docker_available: true
```

Повторный resumable conversion:

```bash
cd /mnt/ii_models/Users/hizhenkov/convert

./scripts/convert_pdf_batches.py \
  "/filer/wps/wp/WP005C_MRO/ECAR_Orders/ECAR_Data/01_TECHNICAL DATA/01_AIRBUS/MAINTENANCE DATA/A320FAM/MPD A320/MPDA320V1_R50_I00.pdf" \
  --total-pages 2182 \
  --batch-size 50 \
  --output-name MPDA320V1_R50_I00
```

Без `--force` команда должна resume/skip completed batches.

## Данные и runtime artifacts

### Full MPD Markdown

```text
/mnt/ii_models/Users/hizhenkov/convert/data/output/MPDA320V1_R50_I00/_full_markdown/MPDA320V1_R50_I00.full.md
```

### Full MPD conversion manifest

```text
/mnt/ii_models/Users/hizhenkov/convert/data/output/MPDA320V1_R50_I00/_full_markdown/manifest.json
```

### Docs KB runtime DB

```text
/mnt/ii_models/Users/hizhenkov/mro_docs_kb/data_runtime/mro_docs_kb.sqlite3
```

Эта SQLite DB содержит полный импорт MPD на момент отчёта. Она не является заменой целевого PostgreSQL, но пригодна для текущего OpenWebUI smoke testing.

### Qdrant collection

```text
mro_docs_chunks
```

Qdrant endpoint:

```text
http://127.0.0.1:6333
```

Embedding endpoint:

```text
http://127.0.0.1:11434/v1/embeddings
```

Embedding model:

```text
bge-m3:latest
```

## Команды проверки текущего состояния

```bash
ss -ltnp | rg ':8095|:8121|:8122|:8131|:6333|:11434'
```

```bash
systemctl --user status mro-kb-api.service mro-ata-impact.service mro-docs-kb-test.service --no-pager -l
```

```bash
docker ps --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}' | rg 'convert|qdrant|ollama|intranet'
```

```bash
curl http://127.0.0.1:8121/v1/models
curl http://10.100.112.51:8122/v1/models
curl http://127.0.0.1:8131/api/health
curl http://127.0.0.1:8095/health
```

## Команды восстановления/отката на состояние 2026-07-31

### `mro_kb_platform`

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_kb_platform
git fetch origin
git checkout platform/experiment
git reset --hard 7995331
systemctl --user restart mro-kb-api.service
systemctl --user restart mro-ata-impact.service
```

Проверить:

```bash
curl http://127.0.0.1:8121/v1/models
curl http://10.100.112.51:8122/v1/models
```

### `mro_docs_kb`

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_docs_kb
git fetch origin
git checkout experiment/mro-similar-cases-structured-profiles
git reset --hard b471abf
```

Перезапуск текущего test service:

```bash
systemctl --user stop mro-docs-kb-test.service || true

systemd-run --user \
  --unit=mro-docs-kb-test \
  --collect \
  --property=WorkingDirectory=/mnt/ii_models/Users/hizhenkov/mro_docs_kb \
  --setenv=QDRANT_URL=http://127.0.0.1:6333 \
  --setenv=QDRANT_COLLECTION=mro_docs_chunks \
  --setenv=EMBEDDING_BASE_URL=http://127.0.0.1:11434 \
  --setenv=EMBEDDING_MODEL=bge-m3:latest \
  --setenv=VECTOR_TOP_K=10 \
  --setenv=HYBRID_TOP_K=20 \
  python3 -m apps.api.server --host 0.0.0.0 --port 8131
```

Если SQLite runtime DB потеряна, повторить импорт полного manifest:

```bash
cd /mnt/ii_models/Users/hizhenkov/mro_docs_kb

python3 - <<'PY'
from pathlib import Path
from ingest.knowledge_base.converted_manifest import ingest_converted_manifest
from storage.knowledge_base.repository import SQLiteDocsKbRepository

repo = SQLiteDocsKbRepository(Path("data_runtime/mro_docs_kb.sqlite3"))
repo.initialize()
result = ingest_converted_manifest(
    repo,
    Path("/mnt/ii_models/Users/hizhenkov/convert/data/output/MPDA320V1_R50_I00/_full_markdown/manifest.json"),
)
print({k: (str(v) if k == "metadata" else v) for k, v in result.items()})
print(repo.stats())
PY
```

Ожидаемо:

```text
pages: 2182
chunks: 5836
batches: 44
```

### `convert_md`

```bash
cd /mnt/ii_models/Users/hizhenkov/convert
git fetch origin
git checkout main
git reset --hard 27b41bf
docker compose up -d
```

Проверить:

```bash
curl http://127.0.0.1:8095/health
```

Если full Markdown потерян, повторить batch conversion:

```bash
./scripts/convert_pdf_batches.py \
  "/filer/wps/wp/WP005C_MRO/ECAR_Orders/ECAR_Data/01_TECHNICAL DATA/01_AIRBUS/MAINTENANCE DATA/A320FAM/MPD A320/MPDA320V1_R50_I00.pdf" \
  --total-pages 2182 \
  --batch-size 50 \
  --output-name MPDA320V1_R50_I00
```

## Что считается стабильным на 2026-07-31

Стабильно:

- `8121` как API для `mro-kb`, `mro-similar-cases`, `mro-go-no-go`;
- `8122` как отдельный правильный `mro-ata-impact`;
- `8095` как Docker `convert-api`;
- full Markdown для `MPDA320V1_R50_I00.pdf`;
- импорт полного MPD в `mro_docs_kb` SQLite runtime;
- OpenWebUI chat с `mro-docs-kb` на `8131`;
- exact/lexical поиск по полному MPD;
- русский smoke prompt `перечисли таски для закрылков` больше не возвращает только первые страницы.

Нестабильно / требует следующего этапа:

- production PostgreSQL для `mro_docs_kb`;
- постоянный systemd unit для `mro-docs-kb`;
- полная Qdrant reindex процедура для всех `5836` chunks;
- строгий MPD table parser;
- reranking task rows;
- distinction между MPD task number, AMM reference, SB number и случайными numeric references;
- нормальная интеграция `mro-docs-kb` evidence в `mro-ata-impact`.

## Риски

- `mro-docs-kb-test.service` transient: после перезагрузки может исчезнуть.
- SQLite runtime DB не является production-хранилищем.
- Qdrant collection может содержать неполную/устаревшую vector index версию.
- Текущий ответ `перечисли таски...` отдаёт candidates, а не полностью валидированную таблицу task rows.
- В `mro_docs_kb` часть кода всё ещё унаследована от platform API и показывает лишние models в `/v1/models`; это не должно быть production-состоянием отдельного сервиса.

## Минимальный smoke после восстановления

```bash
curl http://127.0.0.1:8121/v1/models
curl http://10.100.112.51:8122/v1/models
curl http://127.0.0.1:8131/api/health
curl http://127.0.0.1:8095/health
```

```bash
curl -X POST http://127.0.0.1:8131/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mro-docs-kb","messages":[{"role":"user","content":"перечисли таски для закрылков"}],"stream":false}'
```

Ожидаемый признак успешного восстановления:

```text
Ответ содержит A320 FAMILY MPD / Revision 50 и MPD task candidates с page/section.
```
