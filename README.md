# Forge backend

Минимальный FastAPI backend для проекта Forge. Требуется Python 3.11 или новее.

Документация проекта — [архитектура Week 1](docs/ARCHITECTURE.md), [учебное руководство Week 1](docs/LEARNING_GUIDE.md), [учебное руководство Week 2](docs/LEARNING_GUIDE_WEEK2.md), [учебное руководство Week 3](docs/LEARNING_GUIDE_WEEK3.md), [ADR-001](docs/adr/ADR-001.md), [ADR-002](docs/adr/ADR-002.md) и [backlog](backlog.md).
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

`POST /tasks` создаёт и сохраняет Task. Внутренний `AgentService.execute_run(run_id)` принимает UUID уже существующего `queued` AgentRun. Финальный тест Week 3 вызывает настоящий `TaskService.create_task()`, затем явно создаёт связанный AgentRun в тестовом setup. Публичный API предоставляет работу с Task и Workspace. Scheduler, автоматическое создание AgentRun и HTTP-интерфейс запуска находятся за пределами текущей реализации. [Разбор полного прохода](docs/LEARNING_GUIDE_WEEK3.md#16-полный-проход-task--agentrun)

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

### Учёт usage модели

Week 3 считает usage на двух границах `model.ainvoke()` в custom StateGraph. Research регистрируется сразу после проверки бюджета. Structured finalization регистрируется до проверки envelope, парсинга и финальных инвариантов. Бюджеты по-прежнему ограничивают исполнение графа. Одна usage-запись соответствует одному начатому вызову Runnable. Число HTTP-запросов зависит от реализации Runnable и провайдера.

Runtime читает только нормализованную `AIMessage.usage_metadata`. Для финализации берётся сообщение `raw` из envelope, даже если дальнейшая проверка результата завершается ошибкой. `reported` требует трёх неотрицательных целых счётчиков и `total_tokens == input_tokens + output_tokens`. Нули считаются валидно сообщёнными значениями. `missing` означает отсутствие сообщения или metadata, `invalid` означает некорректные основные счётчики. Валидные `input_token_details.cache_read` и `output_token_details.reasoning` сохраняются отдельно в пределах соответствующих основных счётчиков. Они уже входят в totals. Некорректная детализация отбрасывается без потери валидных основных счётчиков. Проверка относится к данным на границе LangChain.

`AgentService.execute_run()` открывает отдельный scope после успешного claim и проверки отмены. `ContextVar` передаёт collector в задачи графа. Scope ограничен runner и при выходе восстанавливает контекст вызывающего кода без двойного учёта. После закрытия дочерняя задача не может обновить collector. Вызовы, для которых scope закрылся до получения ответа, остаются `started` с неизвестным usage. Для прямого вызова runner caller открывает scope явно и может прочитать snapshot даже при исключении.

```python
from app.services.agent_usage import capture_agent_usage

usage = None
try:
    with capture_agent_usage(request.run_id) as usage:
        result = await runner.run(request)
finally:
    if usage is not None:
        snapshot = usage.snapshot()
        # Snapshot доступен здесь и при ошибке runner. Исключение пробрасывается дальше.
```

Отклонённый графом ответ сохраняет наблюдавшийся usage, а прерванный или ошибочный вызов без ответа оставляет неизвестные токены. `known_input_tokens`, `known_output_tokens` и `known_total_tokens` суммируют только `reported`. `usage_complete` означает, что все наблюдавшиеся вызовы сообщили валидные данные. Пустой scope даёт ноль наблюдавшихся вызовов и нулевые known totals. Его полнота относится только к пустому набору.

### Оценка стоимости и сохранение usage

`AgentService(..., pricing=AgentPricing(research=..., finalization=...))` принимает отдельные неизменяемые тарифы `ModelTariff`. Caller доверенно связывает provider, model и версию тарифа с фазой. Forge использует эту конфигурацию без проверки биллинга фактического вызова. Ставки задаются через `Decimal` в USD за миллион токенов, с точностью до шести знаков после точки. Без явного тарифа стоимость остаётся неизвестной.

Стоимость каждого вызова вычисляется по формуле `(uncached_input × input_rate + cached_input × cached_rate + output × output_rate) / 1_000_000`. Reasoning уже входит в output. При положительном cached input нужна cached rate. Если разбивка неизвестна и input положителен, оценка возможна только при равных известных input и cached rates. Нулевой input и явно нулевой cached count не требуют cached rate. Вход или выход свыше `10**12` исключает оценку вызова, сохраняя его token record. Расчёт сохраняет точность до 12 дробных знаков без округления до центов.

`known_cost_usd` суммирует полностью оценённые вызовы. `cost_complete` требует хотя бы одного наблюдавшегося вызова и оценки каждого из них. Полнота token usage и полнота оценки стоимости проверяются отдельно. Сумма представляет оценку по заданному тарифу. Forge не сверяет её со счётом провайдера и не гарантирует нижнюю или верхнюю границу биллинга.

После результата, ошибки, deadline или отмены сервис отдельной транзакцией записывает один `agent_usage_recorded` с `schema_version`, вызовами и историческими тарифами в виде десятичных строк. Token totals и оценка вычисляются при чтении. `AgentService.get_run_usage(run_id)` возвращает запись с исходной конфигурацией независимо от текущих тарифов сервиса. Для существующего run без события он возвращает `None`, для отсутствующего run — `AgentRunNotFoundError`. Пустой scope и проигравший claim не создают события. Запись после отмены сохраняет terminal status. Повтор идентичной записи идемпотентен, конфликтующая запись отклоняется под блокировкой AgentRun.

Ошибка записи usage после успешного lifecycle даёт `AgentUsagePersistenceError("usage_persistence_failed")` с причиной записи. При уже возникшей ошибке, включая `CancelledError`, сервис пробрасывает исходное исключение с заметкой `agent_usage_persistence_failed`. Закоммиченный lifecycle остаётся в силе. Сбой процесса или повторная внешняя отмена до commit могут оставить usage незаписанным.

### Добавление AgentEvent

Все production-записи AgentEvent проходят через `AgentEventRepository.append(run_id, event_type=..., payload=...)`. Результат, ошибка исполнения и usage вызывают `append()` напрямую. Смена статуса вызывает `add_status_changed()`, который делегирует в `append()`. Caller передаёт непустой тип события длиной до 100 символов и JSON-совместимый словарь. Родительский AgentRun должен уже существовать или быть явно flushed в той же транзакции.

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

Логгер `forge.http` пишет одну JSON-строку в стандартный поток ошибок для каждого HTTP-запроса. Она содержит UTC-время, уровень, событие `http_request_completed`, UUID запроса, метод, шаблон маршрута, HTTP-статус и длительность в миллисекундах. Успешные и обработанные ответы содержат тот же UUID в `X-Request-ID`. При непредвиденном исключении заголовок не гарантирован. Для неизвестного маршрута в поле `route` записывается `unmatched`.

## Логи исполнения агента

`forge.agent` пишет отдельные JSON-строки в `stderr` процесса, который вызывает `AgentService.execute_run()` или `AgentService.cancel_run()`. Каждый вызов получает новый `operation_id`. По `run_id` можно сопоставить исполнение и отмену одного запуска. У исполнения `task_id` появляется в логе завершения успешного claim. До claim и в операциях отмены поле равно `null`.

Пары `agent_operation_started` / `agent_operation_finished` охватывают весь вызов, включая очистку локального handle. Пары `agent_phase_started` / `agent_phase_finished` ограничивают отдельные фазы, включая ожидание операций и соответствующую очистку. `timestamp` берётся по настенным часам UTC, `duration_ms` измеряется монотонными часами.

В `outcome` записывается `completed` при нормальном возврате, `interrupted` при `CancelledError` и `error` при исключении. Даже `blocked`-результат может вернуть `completed`. Статус AgentRun и durable `AgentEvent` в PostgreSQL остаются источником истины. Успешная фаза `cancellation_signal` означает, что сервис обратился к локальному реестру. Ошибка `cancellation_persistence` возможна после commit, если закрытие сессии завершилось исключением.

`agent_failure_classified` содержит стабильный `failure_code`, например `execution_deadline_exceeded`. Ошибки операции и фаз содержат только категорию `error_kind`: `run_not_found`, `stale_run_status`, `invalid_transition` или `operation_error`. Эти записи имеют уровень `WARNING`. Обычные, прерванные и usage-записи имеют уровень `INFO`. В схему входят только разрешённые поля. Prompts, ответы модели, параметры инструментов, исключения, traceback и credentials в лог не попадают.

`agent_usage_observed` появляется перед отдельной записью usage. Он содержит наблюдавшиеся totals и известную часть стоимости, а флаги `usage_complete` и `cost_complete` показывают полноту. Сохранение подтверждает durable `agent_usage_recorded`. В локальном прогоне с синтетическими данными получены такие строки.

```jsonl
{"timestamp":"2026-11-15T15:02:12.396Z","level":"INFO","event":"agent_phase_finished","operation_id":"d3aa3c67-1b15-42db-a5c2-a7ddc97cb332","operation":"execute","run_id":"00000000-0000-0000-0000-000000000001","task_id":"00000000-0000-0000-0000-000000000002","phase":"claim","outcome":"completed","duration_ms":0.049417,"error_kind":null}
{"timestamp":"2026-11-15T15:02:12.396Z","level":"INFO","event":"agent_usage_observed","operation_id":"d3aa3c67-1b15-42db-a5c2-a7ddc97cb332","operation":"execute","run_id":"00000000-0000-0000-0000-000000000001","task_id":"00000000-0000-0000-0000-000000000002","model_calls_started":2,"reported_calls":1,"unknown_usage_calls":1,"known_input_tokens":12,"known_output_tokens":8,"known_total_tokens":20,"usage_complete":false,"priced_calls":1,"unknown_cost_calls":1,"known_cost_usd":"0.000082","cost_complete":false}
```

Запись выполняется синхронно и может ждать медленный `stderr`. При сбое sink отдельная запись может потеряться, исход операции сохраняется. Логи остаются локальными, без distributed tracing и metrics exporter. Смотрите `stderr` процесса, который исполняет или отменяет run. HTTP health-запрос создаёт только запись `forge.http`.

Из `backend` с проектным окружением можно проверить JSON, параллельные операции и отказ sink без вызовов провайдера.

```bash
.venv/bin/python -m pytest -v tests/test_agent_observability.py tests/test_logging.py
```

Тест `test_agent_service_observability_records_real_graph` из PostgreSQL набора ниже использует реальный StateGraph, `read_fixture` и синтетические ответы моделей. Чтобы выполнить только эти четыре сценария в существующей выделенной базе, задайте `POSTGRES_DB`, `POSTGRES_HOST` и `FORGE_TASK06_TEST_DB` и запустите команду.

```bash
.venv/bin/python -m pytest -v tests/test_agent_service_integration.py -k test_agent_service_observability_records_real_graph
```

## Тесты

Из `backend` запустите обычный pytest в проектном `.venv`. Отдельный subprocess изолирует PostgreSQL opt-ins от текущего shell.

```bash
(
  unset FORGE_TEST_POSTGRES FORGE_TEST_SCHEMA_DB FORGE_TASK04_TEST_DB FORGE_TASK05_TEST_DB FORGE_TASK06_TEST_DB
  .venv/bin/python -m pip check &&
  .venv/bin/python -m pytest -q -rs
)
```

### Полная проверка PostgreSQL для Week 3

Из корня репозитория проверьте локальный Compose и порт. Существующий `.env` задаёт пользователя и пароль PostgreSQL. Подставьте свой локальный порт, если он отличается от `55432`.

```bash
docker compose ps postgres
docker compose port postgres 5432
```

Следующий блок создаёт четыре свежие базы для тестов и мигрирует каждую к `1d972cc34fbe (head)`. `createdb` использует системную базу `postgres` как maintenance database. Если имя уже занято, команда останавливает блок. Базы и volume после проверки сохраняются. Для повторного Step 10 используйте существующие имена из таблицы ниже и пропустите создание. Сначала проверьте имя базы, isolation и revision.

```bash
(
  set -e
  stamp=$(date -u +%Y%m%d_%H%M%S)
  task_schema_db="forge_task03_week3_final_$stamp"
  repository_db="forge_task04_week3_final_$stamp"
  execution_schema_db="forge_task05_week3_final_$stamp"
  lifecycle_db="forge_task06_week3_result_$stamp"
  export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432
  export LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
  export LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_HANDLER=false
  unset FORGE_TEST_POSTGRES FORGE_TEST_SCHEMA_DB FORGE_TASK04_TEST_DB FORGE_TASK05_TEST_DB FORGE_TASK06_TEST_DB

  for test_database in "$task_schema_db" "$repository_db" "$execution_schema_db" "$lifecycle_db"; do
    docker compose exec -T postgres sh -ec \
      'exec createdb --maintenance-db=postgres -U "$POSTGRES_USER" "$1"' sh "$test_database"
    (
      cd backend
      POSTGRES_DB="$test_database" .venv/bin/python -m alembic upgrade head
    )
    docker compose exec -T postgres sh -ec \
      'exec psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -c "SELECT current_database(); SHOW transaction_isolation; SHOW server_version;"' \
      sh "$test_database"
    (
      cd backend
      POSTGRES_DB="$test_database" .venv/bin/python -m alembic current
      POSTGRES_DB="$test_database" .venv/bin/python -m alembic check
    )
  done

  cd backend
  POSTGRES_DB="$lifecycle_db" FORGE_TASK06_TEST_DB="$lifecycle_db" \
    .venv/bin/python -m pytest -v \
      tests/test_agent_service_integration.py \
      tests/test_agent_result_persistence_integration.py \
      tests/test_agent_event_append_integration.py \
      tests/test_agent_run_integration.py \
      tests/test_task_transition_integration.py \
      tests/test_task_idempotency_integration.py

  POSTGRES_DB="$lifecycle_db" FORGE_TEST_POSTGRES=1 \
    .venv/bin/python -m pytest -v tests/test_postgres_integration.py
  POSTGRES_DB="$task_schema_db" FORGE_TEST_SCHEMA_DB="$task_schema_db" \
    .venv/bin/python -m pytest -v tests/test_schema_integration.py
  POSTGRES_DB="$repository_db" FORGE_TASK04_TEST_DB="$repository_db" \
    .venv/bin/python -m pytest -v tests/test_repository_integration.py
  POSTGRES_DB="$execution_schema_db" FORGE_TASK05_TEST_DB="$execution_schema_db" \
    .venv/bin/python -m pytest -v tests/test_execution_schema_integration.py
)
```

У всех наборов свои opt-ins. Для `FORGE_TASK05_TEST_DB` и `FORGE_TASK06_TEST_DB` обязательно совпадение с `POSTGRES_DB`. Schema tests откатывают записи, а lifecycle и repository tests чистят созданные ими записи по UUID. Проверки конкуренции используют `READ COMMITTED` и подтверждают ожидание блокировки через `pg_blocking_pids()`.

| Группа | Выделенная база Step 10 | Итог |
|---|---|---:|
| Lifecycle, результат, события, переходы и idempotency | `forge_task06_week3_result_20261114_123241` | 151 passed |
| Engine и session | `forge_task06_week3_result_20261114_123241` | 2 passed |
| Task schema и rollback | `forge_task03_week3_final_20261115_161856` | 2 passed |
| HTTP/repository lifecycle и rollback | `forge_task04_week3_final_20261115_161856` | 1 passed |
| Execution schema и rollback | `forge_task05_week3_final_20261115_161856` | 1 passed |

В Step 10 все четыре базы подтвердили PostgreSQL 17.11, `READ COMMITTED`, `alembic current` на `1d972cc34fbe (head)` и `alembic check` без новых операций. Обычный прогон дал `749 passed, 157 skipped` с opt-ins выключенными. Все 157 покрыты отдельными прогонами. Суммарно 906 уникальных случаев, 0 итоговых failures и 0 pytest warnings.

#### Повторить только E2E

Из `backend` выполните одну команду с уже мигрированной выделенной базой Step 10. Тест использует настоящие TaskService, StateGraph и ChatOpenAI, а HTTPX MockTransport отвечает синтетическими данными. Тест проверяет durable result, history и usage через новый engine. Внешняя отправка модели и tracing в этом тесте отключены.

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 \
POSTGRES_DB=forge_task06_week3_result_20261114_123241 \
FORGE_TASK06_TEST_DB=forge_task06_week3_result_20261114_123241 \
.venv/bin/python -m pytest -v \
  tests/test_agent_service_integration.py::test_week3_task_to_agent_run_end_to_end
```
