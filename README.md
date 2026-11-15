# Forge backend

Минимальный FastAPI backend для проекта Forge. Требуется Python 3.11 или новее.

Документация: [архитектура Week 1](docs/ARCHITECTURE.md), [учебное руководство Week 1](docs/LEARNING_GUIDE.md), [учебное руководство Week 2](docs/LEARNING_GUIDE_WEEK2.md), [ADR-001](docs/adr/ADR-001.md), [backlog](backlog.md).
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

Обычный запуск тестов из `backend` не требует PostgreSQL и пропускает opt-in интеграционные тесты. Чтобы отдельно проверить соединение и `SELECT current_database()`, сначала запустите Compose из корня, затем из `backend` выполните:

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

Связи: `Task → AgentRun → AgentEvent / ToolCall / Artifact`, а также `ToolCall → Approval`. Внешние ключи используют `ON DELETE RESTRICT`. Номера попыток уникальны в пределах Task, номера событий — в пределах AgentRun. Оба значения положительны. CHECK ограничивает значения статусов и запрещает пустые обязательные строки. Время хранится с часовым поясом, payload события — в JSONB.

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

## Переходы состояний в PostgreSQL

`AgentRunService.transition_status()` блокирует строку AgentRun через `SELECT FOR UPDATE`, сравнивает сохранённый статус с ожидаемым и проверяет допустимость перехода. Изменение статуса и `AgentEvent` типа `agent_run_status_changed` с полями `from` и `to` фиксируются одной транзакцией. При ошибке оба изменения откатываются. Терминальные состояния `succeeded`, `failed` и `cancelled` нельзя переоткрыть. Для успешного завершения требуется независимое подтверждение результата, поэтому сервис пока отклоняет переход в `succeeded`.

`TaskService.transition_status()` также сохраняет статус в PostgreSQL под блокировкой строки и отклоняет запрос с устаревшим ожидаемым статусом. У Task собственный граф состояний. Переход AgentRun сам по себе не меняет Task. `verifying → running` допустим для исправления, а переход в `succeeded` заблокирован до появления независимого подтверждения результата. Оба метода перехода доступны на уровне сервисов. HTTP API пока не публикует операции перехода.

`AgentService` захватывает подготовленный AgentRun и закрывает claim-сессию до вызова `AgentRunner`. Текущий LangGraph runtime поддерживает ограниченный read-only `read_fixture`. Обработка approvals, оркестрация retry/resume и независимая verification относятся к последующим этапам.

### Deadline исполнения агента

По умолчанию `AgentService` выделяет **60 секунд** на весь `AgentRunner.run()`, включая исследовательские вызовы модели, инструменты и structured finalization. Переопределить лимит можно при создании сервиса через `AgentService(factory, runner, execution_timeout_seconds=30)`. Принимаются положительные конечные `int` и `float`. Deadline начинается после commit и закрытия claim-сессии. Валидация результата, сохранение итогового состояния и остальные операции PostgreSQL находятся за его пределами.

Таймер запроса провайдера действует на отдельный запрос модели и по умолчанию составляет 15 секунд. При собственном deadline сервис выдаёт `AgentRunnerError("execution_deadline_exceeded")`. Более ранние ошибки runner, включая его собственный timeout, сохраняют исходный тип и причину. Автоматических повторов нет.

При истечении deadline run в `running` переходит в `failed` с событием `agent_execution_failed` между двумя событиями смены статуса. `final_result` остаётся SQL NULL. Если отдельная транзакция уже зафиксировала `cancelled`, сервис сохраняет этот статус и пробрасывает `StaleAgentRunStatusError` с ошибкой deadline в цепочке причин. Внешний `CancelledError` пробрасывается без автоматической смены статуса, поэтому run может остаться в `running`.

Deadline использует кооперативную отмену asyncio. Блокировка event loop или игнорирование отмены могут задержать завершение. Сервис отклоняет результат, вернувшийся после deadline. Отмена уже отправленного внешнего действия, принудительное завершение процесса и восстановление после сбоя находятся за пределами этой гарантии.

### Явная отмена AgentRun

`AgentRunService.cancel_execution(run_id)` блокирует строку и атомарно сохраняет переход в `cancelled` вместе с `agent_run_status_changed`. Допустимые исходные статусы — `queued`, `running`, `waiting_approval` и `verifying`. Повтор для `cancelled` успешен без нового события. Для отсутствующего run сервис выдаёт `AgentRunNotFoundError`, для `failed` и `succeeded` — `InvalidAgentRunTransition`. Отмена сохраняет поля Task, номер попытки, ранее записанный результат и историю. Failure event при отмене не создаётся.

`AgentService.cancel_run(run_id)` выполняет доменную отмену в отдельной сессии. Он передаёт сигнал в `ActiveRunRegistry` после подтверждённого commit и выхода из контекста сессии. Ошибка закрытия сессии после commit пробрасывается, а сигнал всё равно отправляется. При ошибке записи или неподтверждённом commit сигнал не отправляется.

По умолчанию у каждого `AgentService` свой реестр. Экземпляры в одном процессе и одном event loop могут явно разделять `ActiveRunRegistry` через параметр `active_runs`. Каждый `execute_run()` регистрирует свой handle до claim и удаляет его в `finally`. Для одного run одновременно могут существовать несколько ожидающих claim handles. Право выполнить runner определяет PostgreSQL. Реестр вызывает `Task.cancel()` один раз на handle только внутри runner phase. Во время claim, completion или записи ошибки он ставит флаг без прерывания транзакции. Если отмена отмечена до начала runner, runner не запускается. Если runner подавил явную отмену и вернул результат или бросил обычное исключение, сервис завершает вызов с `CancelledError` без записи этого исхода.

В гонке порядок определяет блокировка строки PostgreSQL. Если доменная отмена зафиксирована первой, completion и запись ошибки отклоняются по статусу. Если первым завершился ready completion, последующая отмена переводит `verifying → cancelled` и сохраняет предварительный результат. Если первой зафиксирована ошибка исполнения, отмена из `failed` отклоняется, а metadata сохраняется. Deadline продолжает записывать `execution_deadline_exceeded`, когда статус допускает переход в `failed`. Внешняя отмена coroutine без `cancel_run()` не меняет доменный статус и может оставить run в `running`.

Успешный `cancel_run()` подтверждает commit и попытку доставки локального сигнала. Cleanup runner может ещё выполняться. Сигнал ограничен одним процессом и event loop. Распределённая доставка, принудительная остановка, отзыв внешних действий, отмена verification worker и durable recovery за пределами этой операции.

### Предварительный результат AgentRun

Миграция `1d972cc34fbe` добавляет nullable JSONB-колонку `agent_runs.final_result`. Версия хранения 1 использует объект `{"schema_version": 1, "result": {...}}`. В `result` входят `summary`, `status`, `relevant_components`, `assumptions`, `observations` и `blocked_reason`. Codec валидирует результат и вложенные observations по существующему строгому контракту. При чтении JSON-массивы восстанавливаются как tuple.

После claim на новом run событие `queued → running` получает sequence 1. `complete_execution()` под блокировкой AgentRun проверяет `running` и отсутствие записанного результата. Один commit сохраняет JSONB, событие `agent_result_recorded` (sequence 2, payload только `schema_version` и `result_status`), новый статус и событие перехода (sequence 3). `ready_for_verification` ведёт в `verifying`, а `blocked` ведёт в `failed` с сохранением результата.

Любой новый переход в `verifying` требует сохранённого валидного `ready_for_verification`. При отменённом или уже завершённом run позднее completion отклоняется. `AgentService.get_run_result()` читает через новую сессию без вызова runner. Для SQL NULL, включая исторические `verifying`, он возвращает `None`. Отсутствующий run и повреждённый документ дают отдельные ошибки. Исторические строки сохраняются без backfill. Предварительный результат доступен для независимой verification, которая по-прежнему требуется для `succeeded`. Хранение завершённого результата не обеспечивает recovery и resume исполнения.

### Ошибки исполнения и retry disposition

`LangGraphAgentRunner` сопоставляет тип ошибки SDK или графа со стабильным кодом и сохраняет исходное исключение как `__cause__`. `AgentService` принимает известные коды из `AgentRunnerError.reason`, классифицирует прямой `TimeoutError` как `runner_timeout`, а неизвестную ошибку или reason как `execution_failed`. Классификация не использует сообщения и цепочки исключений.

| Условие | Код | Retry disposition |
| --- | --- | --- |
| Общий deadline сервиса | `execution_deadline_exceeded` | `review_required` |
| Обычный `TimeoutError` | `runner_timeout` | `review_required` |
| SDK timeout или HTTP 408 | `provider_timeout` | `transient_candidate` |
| SDK connection error | `provider_connection` | `transient_candidate` |
| HTTP 429, код `rate_limit_exceeded` | `provider_rate_limited` | `transient_candidate` |
| HTTP 429, код `insufficient_quota` | `provider_quota_exhausted` | `not_retryable` |
| HTTP 429, другой или отсутствующий код | `provider_rate_limit_unknown` | `review_required` |
| HTTP 401 или 403 | `provider_auth` | `not_retryable` |
| HTTP 400, 404 или 422 | `provider_request_rejected` | `not_retryable` |
| HTTP 500–599 | `provider_unavailable` | `transient_candidate` |
| Прочие SDK API errors | `provider_error` | `review_required` |
| Лимит рекурсии, модели, инструментов или финализации | `graph_budget_exceeded` | `review_required` |
| Прочий `AgentGraphError` | `graph_execution_failed` | `review_required` |
| `StructuredOutputError` или `FinalizationError` | `structured_output_invalid` | `review_required` |
| Runner вернул результат вне контракта | `invalid_runner_result` | `review_required` |
| Неизвестная ошибка или неизвестный reason | `execution_failed` | `review_required` |

`transient_candidate` указывает на потенциально временный сбой. `not_retryable` означает, что повтор при прежних условиях бессмысленен. `review_required` требует отдельного решения. Ни один из этих исходов не запускает retry, не создаёт новую попытку и не обеспечивает восстановление исполнения.

После claim история ошибки состоит из `queued → running`, `agent_execution_failed` и `running → failed` с sequence 1, 2, 3. Под блокировкой AgentRun одна транзакция сохраняет failure event, новый статус и status event. `final_result` остаётся SQL NULL. Payload failure event содержит только `{"schema_version": 1, "code": "<stable code>"}`. Текст исключения, traceback, HTTP-данные, raw model output и retry disposition в event не попадают.

`AgentService.get_run_failure()` читает metadata через новую сессию без вызова runner. При отсутствии AgentRun возвращается ошибка поиска. Если события нет, метод возвращает `None`, в том числе для исторического `failed` и сохранённого `blocked` result. Повреждённое, неизвестной версии или дублированное событие даёт ошибку контракта. Retry disposition вычисляется из прочитанного кода.

### Добавление AgentEvent

Все production-записи AgentEvent проходят через `AgentEventRepository.append(run_id, event_type=..., payload=...)`. Запись результата и ошибки исполнения вызывает `append()` напрямую. Смена статуса вызывает `add_status_changed()`, который делегирует в `append()`. Caller передаёт непустой тип события длиной до 100 символов и JSON-совместимый словарь. Родительский AgentRun должен уже существовать или быть явно flushed в той же транзакции.

`append()` блокирует ID родительского AgentRun через `SELECT FOR UPDATE` с отключённым autoflush. После получения блокировки репозиторий явно выполняет `flush()` всего текущего unit of work, включая предыдущие pending events. Затем отдельный запрос `COALESCE(MAX(sequence), 0) + 1` выделяет номер в пределах этого AgentRun. При PostgreSQL `READ COMMITTED` отдельный запрос получает свежий snapshot и видит события, зафиксированные за время ожидания блокировки.

Caller владеет commit и rollback и держит короткую транзакцию для одного AgentRun. Репозиторий возвращает новый pending event, а блокировка сохраняется до конца транзакции. После rollback номер может использоваться повторно. `UNIQUE(agent_run_id, sequence)` остаётся финальной защитой. Гарантии этого контракта ограничены транзакционной записью и нумерацией. Event sourcing, доставка событий и durable workflow за его пределами.

## Идемпотентное создание Task

`POST /tasks` принимает необязательный заголовок `Idempotency-Key` со значением UUID. Ключ действует в пределах Workspace. Для сравнения запроса сервис убирает пробелы по краям `goal` и каждого элемента `acceptance_criteria`. Порядок критериев учитывается.

| Запрос | Результат |
| --- | --- |
| Первое создание с ключом | HTTP `201`, новая Task |
| Тот же Workspace, ключ и нормализованные данные | HTTP `201`, исходный ID Task |
| Тот же Workspace и ключ, другие данные | HTTP `409`, код `IDEMPOTENCY_KEY_CONFLICT` |
| Некорректный UUID в заголовке | HTTP `422` |
| Валидный запрос без заголовка | HTTP `201`, новая Task при каждом запросе |

При конкурентных запросах PostgreSQL UNIQUE по `(workspace_id, idempotency_key)` не допускает дубликаты. Если `INSERT … ON CONFLICT DO NOTHING RETURNING id` не вернул ID, сервис ищет существующую Task отдельным SELECT. При `READ COMMITTED` этот SELECT получает новый snapshot и видит строку, которую успела зафиксировать конкурирующая транзакция. Replay сохраняет текущий статус Task.

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

### PostgreSQL integration tests для Step 06 и Step 07

Создайте новую выделенную базу из корня репозитория при запущенном Compose. Существующую базу Step 07A сохраните для исторической проверки.

```bash
TEST_DB=forge_task06_week3_result_$(date +%Y%m%d_%H%M%S)
docker compose exec -T postgres sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB" &&
(
  cd backend || exit 1
  export POSTGRES_DB="$TEST_DB" POSTGRES_HOST=127.0.0.1 FORGE_TASK06_TEST_DB="$TEST_DB"
  .venv/bin/python -m alembic upgrade head &&
  .venv/bin/python -m alembic current &&
  .venv/bin/python -m alembic check &&
  .venv/bin/python -m pytest -v \
    tests/test_agent_result_persistence_integration.py \
    tests/test_agent_event_append_integration.py \
    tests/test_agent_service_integration.py \
    tests/test_agent_run_integration.py \
    tests/test_task_transition_integration.py \
    tests/test_task_idempotency_integration.py
)
```

`POSTGRES_DB` и `FORGE_TASK06_TEST_DB` должны совпадать, а имя должно соответствовать `forge_task06_[a-z0-9_]+`. Тесты результата проверяют revision `1d972cc34fbe`. Перед записью тесты сверяют `current_database()` и при очистке удаляют только созданные ими записи. Проверки конкуренции используют `READ COMMITTED` и подтверждают ожидание блокировки через `pg_blocking_pids()`.
