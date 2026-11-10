# Forge backend

Минимальный FastAPI backend для проекта Forge. Требуется Python 3.11 или новее.

Документация: [архитектура](docs/ARCHITECTURE.md), [учебное руководство](docs/LEARNING_GUIDE.md), [ADR-001](docs/adr/ADR-001.md), [backlog](backlog.md).
Документ архитектуры описывает состояние Week 1; актуальный запуск PostgreSQL и API приведён ниже.

## Установка

Из директории `backend`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Запуск

Сначала запустите PostgreSQL из корня репозитория, затем примените миграции из `backend` и запустите API:

```bash
# Из корня репозитория, после настройки .env:
docker compose up -d --wait
cd backend
alembic upgrade head
python -m uvicorn app.main:app --reload
```

Проверка API: `http://127.0.0.1:8000/health`. Swagger UI: `http://127.0.0.1:8000/docs`.

Workspace API:

```bash
curl -i -X POST http://127.0.0.1:8000/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"name":"My Workspace"}'
curl -i http://127.0.0.1:8000/workspaces
```

Workspace и Task хранятся в PostgreSQL и доступны после перезапуска приложения. `GET /workspaces` возвращает записи в порядке UUID по возрастанию.

## Локальный PostgreSQL

Для PostgreSQL 17 нужен Docker с Compose. Из корня репозитория создайте локальный файл настроек и замените шаблонный пароль своим значением. Файл `.env` игнорируется Git; `.env.example` содержит только шаблон.

```bash
test -f .env || cp .env.example .env
# Отредактируйте POSTGRES_PASSWORD в .env перед запуском.
docker compose config --quiet
docker compose up -d --wait
docker compose ps
```

База доступна только на `127.0.0.1:55432` (или на порту из `POSTGRES_PORT` в `.env`). Для проверки SQL используйте `psql` внутри контейнера; дополнительная установка на хосте не нужна:

```bash
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT current_database(), 1 AS ok;"'
```

Проверка сохранности данных при пересоздании контейнера:

```bash
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
CREATE TABLE IF NOT EXISTS forge_persistence_check (id integer PRIMARY KEY, marker text NOT NULL);
INSERT INTO forge_persistence_check (id, marker) VALUES (1, 'survived_restart')
ON CONFLICT (id) DO UPDATE SET marker = EXCLUDED.marker;
SELECT marker FROM forge_persistence_check WHERE id = 1;
SQL
docker compose down
docker compose up -d --wait
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT marker FROM forge_persistence_check WHERE id = 1;"'
```

Последний `SELECT` должен вернуть `survived_restart`. Команда `docker compose down` удаляет контейнер и сеть, но сохраняет named volume `postgres_data` с данными. После проверки остановите сервис:

```bash
docker compose down
```

Для работы API PostgreSQL должен быть запущен, а миграции применены отдельно.

## Асинхронное подключение к PostgreSQL

После установки зависимостей `Settings()` читает `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` и необязательный `POSTGRES_PORT` из корневого `.env`, в том числе при запуске из `backend`. Переменные окружения имеют приоритет над файлом. Хост по умолчанию — `127.0.0.1`; при необходимости его можно задать через `POSTGRES_HOST`. Пустые обязательные значения и неверный порт отклоняются до подключения.

Загрузчик `.env` интерполирует последовательности вида `${NAME}` даже в значениях в одинарных кавычках. Для буквального `${...}` в пароле задайте `POSTGRES_PASSWORD` через окружение и для Compose, и для приложения.

`create_engine(settings)` создаёт `AsyncEngine` с драйвером asyncpg и проверкой соединений пула; само подключение открывается при первом обращении. URL собирается из отдельных полей, поэтому специальные символы пароля не ломают его разбор. Lifespan FastAPI создаёт engine и фабрику с `expire_on_commit=False`, а при остановке освобождает engine. Для каждого запроса создаётся отдельная `AsyncSession`, закрываемая после запроса. Сервисы завершают commit до успешного ответа; при ошибке выполняется rollback.

Session управляет работой с соединением и транзакцией: SQL-запрос может неявно начать транзакцию, а для сохранения будущих изменений понадобится явный `commit`. При выходе из контекста session закрывается; незавершённая транзакция откатывается.

Обычный запуск тестов из `backend` не требует PostgreSQL и пропускает интеграционный тест. Чтобы отдельно проверить соединение и `SELECT current_database()`, сначала запустите Compose из корня, затем из `backend` выполните:

```bash
FORGE_TEST_POSTGRES=1 python -m pytest -q tests/test_postgres_integration.py
```

Тест закрывает session и освобождает engine после проверки. При изменении параметров в `.env` учитывайте, что переменные `POSTGRES_*` инициализируют пользователя и базу только при первом создании пустого каталога данных PostgreSQL.

## Миграции PostgreSQL

Из `backend` с установленными зависимостями и запущенным PostgreSQL:

```bash
alembic upgrade head
alembic current
alembic check
```

Alembic использует те же `Settings` и async engine, что и backend; пароль в `alembic.ini` не хранится. Перед запуском проверьте, что настройки указывают на нужную базу. Миграции не запускаются автоматически при старте FastAPI. Autogenerate исключает из кандидатов на удаление таблицы, отсутствующие в metadata, включая таблицы удалённых из кода моделей. Все сгенерированные изменения нужно проверять вручную; одноимённые таблицы считаются принадлежащими модели. Команда `downgrade` начальной revision удаляет `tasks` и `workspaces` с данными.

Для проверки схемы создайте отдельную пустую базу из корня репозитория. Если `createdb` завершится ошибкой, остановитесь и выберите другое имя:

```bash
TEST_DB=forge_task03_$(date +%Y%m%d_%H%M%S)
docker compose exec -T postgres sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
cd backend
POSTGRES_DB="$TEST_DB" alembic upgrade head
POSTGRES_DB="$TEST_DB" alembic upgrade head
POSTGRES_DB="$TEST_DB" alembic current
POSTGRES_DB="$TEST_DB" alembic check
FORGE_TEST_SCHEMA_DB="$TEST_DB" python -m pytest -q tests/test_schema_integration.py
```

Тесты схемы запускаются только при заданном `FORGE_TEST_SCHEMA_DB` с префиксом `forge_task03_` и откатывают свои строки. Тестовая база остаётся на месте; её удаление требует отдельного решения. `alembic check` дополняет, а не заменяет проверки ограничений в PostgreSQL.

## Данные исполнения агента

Task описывает намерение, AgentRun — отдельную попытку исполнения: один Task может иметь несколько AgentRun. У AgentRun есть AgentEvent для структурированных событий, ToolCall для записи вызовов инструментов и Artifact со ссылкой на результат. Approval связан с ToolCall; для одного вызова допускается несколько согласований.

Связи: `Task → AgentRun → AgentEvent / ToolCall / Artifact`, а также `ToolCall → Approval`. Внешние ключи используют `ON DELETE RESTRICT`. Номера попыток уникальны в пределах Task, номера событий — в пределах AgentRun; оба положительны. CHECK ограничивает значения статусов и запрещает пустые обязательные строки. Время хранится с часовым поясом, payload события — в JSONB. Проверки значений статуса не обеспечивают допустимость переходов. Исполнение агента, retry, resume и переходы состояний пока не реализованы.

Для проверки Task 05 создайте отдельную пустую базу с префиксом `forge_task05_`. Из корня репозитория при запущенном Compose и установленных зависимостях выполните:

```bash
TEST_DB=forge_task05_$(date +%Y%m%d_%H%M%S)
if docker compose exec -T postgres sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"; then
  (
    cd backend || exit 1
    export POSTGRES_DB="$TEST_DB" POSTGRES_HOST=127.0.0.1
    .venv/bin/python -m alembic upgrade head &&
    .venv/bin/python -m alembic current &&
    .venv/bin/python -m alembic check &&
    .venv/bin/python -m alembic upgrade head &&
    FORGE_TASK05_TEST_DB="$TEST_DB" .venv/bin/python -m pytest -q tests/test_execution_schema_integration.py
  )
else
  printf 'База не создана; выберите другое имя и повторите.\n' >&2
fi
```

Тест требует совпадения `POSTGRES_DB` и `FORGE_TASK05_TEST_DB`, проверяет префикс и откатывает тестовые записи. Созданная база остаётся на месте.

## Ошибки API

Предусмотренные ошибки возвращаются в едином формате:

```json
{"error":{"code":"VALIDATION_ERROR","message":"Request validation failed","details":[{"field":"body.goal","message":"String is too short"}]}}
```

Для ошибок без сведений о полях `details` — пустой список. Ответы 404 и 405 сохраняют свои HTTP-статусы; ответ 405 также содержит заголовок `Allow`.

## HTTP-логи

Forge пишет одну JSON-строку в стандартный поток ошибок для каждого HTTP-запроса. Она содержит UTC-время, уровень, событие `http_request_completed`, UUID запроса, метод, шаблон маршрута, HTTP-статус и длительность в миллисекундах. Успешные и обработанные ответы содержат тот же UUID в `X-Request-ID`; при непредвиденном исключении заголовок не гарантирован. Для неизвестного маршрута в поле `route` записывается `unmatched`.

## Тесты

Из директории `backend` с активированным виртуальным окружением:

```bash
python -m pytest -q
```

Для opt-in проверки репозиториев создайте отдельную пустую базу из корня репозитория. Если `createdb` завершится ошибкой, остановитесь и выберите другое имя:

```bash
TEST_DB=forge_task04_$(date +%Y%m%d_%H%M%S)
docker compose exec -T postgres sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
cd backend
POSTGRES_DB="$TEST_DB" alembic upgrade head
FORGE_TASK04_TEST_DB="$TEST_DB" python -m pytest -q tests/test_repository_integration.py
```

Тесты принимают только имя с префиксом `forge_task04_` и удаляют только записи с собранными во время теста ID. База остаётся на месте.
