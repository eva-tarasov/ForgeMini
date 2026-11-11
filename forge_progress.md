# AI Product Engineer / Forge — progress.md

## Общие параметры

**Режим:** 4–5 часов в день, 6 дней в неделю  
**Нагрузка:** примерно 24–30 часов в неделю  
**Основная программа:** 12 недель  
**Оценочный объём:** примерно 286–350 часов

---

# Project Governance — Architecture & Codex Workflow

**Добавлено:** 2026-10-21  
**Статус:** обязательные правила процесса, без автоматического изменения roadmap или принятой архитектуры.  
**Source of truth:** этот `progress.md` для milestone/progress; принятые ADR для архитектурных решений; рабочий Git-репозиторий и результаты проверок — для фактического состояния кода.

## 1. Architecture continuity / правило сохранения траектории

1. Следуем текущему 12-недельному roadmap и принятой **ADR-001: modular monolith → durable agent execution**. Приоритет — реализовать текущий milestone и его exit criteria.
2. **Новая технология, фреймворк, статья или предложение агента не являются основанием менять архитектуру автоматически.** Сначала фиксируем, какую конкретную проблему текущая реализация не решает.
3. Любая существенная альтернатива проходит **Architecture Change Gate** до внедрения:
   - текущее решение / baseline и его ограничения;
   - новая альтернатива и решаемая проблема;
   - плюсы, минусы, failure modes, security boundaries;
   - влияние на сложность, стоимость эксплуатации, vendor lock-in, тесты, обучение и сроки;
   - по возможности — небольшой ограниченный spike/benchmark по одинаковым acceptance criteria;
   - рекомендация: `keep current`, `defer`, `run spike`, `adopt`;
   - обновлённый ADR и `progress.md` **только после осознанного решения**.
4. **Adopt** допустим, когда новая технология даёт существенное, подтверждённое преимущество для текущих требований, устраняет реальный blocker, либо закрывает критический security/reliability defect. «Популярно», «появилось в вакансии», «похоже на Forge» — недостаточные основания.
5. Решения по технологиям принимаются на **milestone, в котором возникает потребность**. До этого: исследование и заметка в `Decisions to revisit`, но не переписывание кода.
6. Обязательные инварианты сохраняются при любом изменении стека: `Task ≠ AgentRun`, сервер — источник истины, permissions/approvals до side effects, независимая verification с evidence, terminal state не переоткрывается, audit trail, bounded retries/cost.

## 2. Workflow with Codex (разработка vs продукт)

**Codex — внешний coding agent, который помогает строить Forge. Forge — отдельный продукт с собственными AgentRun, runtime, permissions, sandbox и verification.** Codex не является скрытой заменой этих компонентов.

- **ChatGPT / чат текущей недели:** объяснение концепции, решение архитектуры, постановка задачи и acceptance criteria, review результатов, разбор незнакомого кода.
- **Евгений:** product scope, архитектурные решения, одобрение scope/существенного diff, понимание control flow, data model и failure modes.
- **Codex app / CLI / IDE:** исследование репозитория, точечные изменения, тесты, линт, подготовка diff, отчёт с evidence. Режим LEARNING / ENGINEERING / AGENTIC определяется знакомством с областью, а не возможностями Codex.
- Один шаг = одна ограниченная задача Codex. Не переходить к следующей фазе автоматически. После review и объяснения Евгений пишет **«продолжаем»** либо просит разобрать/изменить текущий шаг.
- Для нетривиальной задачи Codex указываем: `Objective, Context, Scope, Allowed/Forbidden changes, Constraints, Acceptance criteria, Verification, Report format`.
- Не принимать `done` без доказательств: команды, тесты, diff, ошибки, migration checks, screenshots или runtime output — в зависимости от задачи.
- Без явного разрешения не делать push/deploy, не изменять secrets, не выполнять опасные действия на хосте, не менять архитектурные зависимости или ADR.
- На новой учебной теме Codex может писать каркас и узкие участки кода **после объяснения модели**; пользователь обязательно изучает существенные фрагменты до перехода дальше.

## 3. Technology decision gates

- **Week 3 — Agent Runtime:** рассмотреть поддерживаемые варианты `OpenAI Agents SDK`, `LangChain create_agent` и минимальный прямой agent loop (как учебный baseline). Выбирать **один основной runtime**, а не ставить все пакеты сразу. Проверить tool calls, typed outputs, instrumentation, approvals integration, cost, тестируемость.
- **Week 7 — Durable Orchestration:** исходная кандидатура — **Temporal**. Сравнить с **LangGraph** на одинаковом сценарии `execute → approval pause → resume → verification → worker restart`. Проверить recovery semantics, безопасные side effects, checkpoint/replay, сложность deployment и соответствие нашему domain state. При необходимости возможно сочетание разных уровней, но **не подключать оба по умолчанию**.
- **LangSmith / RAG / multi-agent / Deep Agents / Redis / другие фреймворки:** не добавлять без конкретной потребности текущего milestone и прохождения Architecture Change Gate.
- Изменение реализации не должно уничтожать domain model и boundaries без отдельного ADR.

## 4. Weekly execution and status

- Для каждой недели: `design/theory → one scoped implementation task → tests/verification → diff review → explanation → progress update`.
- Статусы `not started / in progress / blocked / done` менять **по фактическому evidence**, не по календарю и не по отчёту агента без проверки.
- Если встречена интересная новая технология — внести её в **Decisions to revisit**, не прерывая текущую неделю.
- При отклонении от roadmap указывать: `why`, `trade-off`, `ADR`, `migration cost`, `effect on exit criteria`.
- В архитектурном чате `00` сначала теория на английском, затем тот же блок по-русски.

## Decisions to revisit

| Decision | Planned gate | Current stance | Required evidence |
|---|---|---|---|
| OpenAI Agents SDK vs LangChain create_agent | Week 3 | no switch until comparison | minimal single-agent run, tools, structured output, tracing, tests |
| LangGraph vs Temporal | Week 7 | Temporal is starting candidate, not irreversible choice | restart/recovery, approval/resume, idempotent side effects, complexity |
| Extra layers/microservices/multi-agent | When actual blocker appears | deferred | real operational or product need, benchmark/ADR |

---

# 0. Product Scope

## Главная цель

За 12 недель построить portfolio-grade AI-native продукт:

**backend -> agent -> tools -> MCP -> sandbox -> durable execution -> verification/evals -> Apple client -> production**

Forge должен демонстрировать не просто вызов LLM, а полноценную agentic system.

---

## Core Use Case v1

Forge выполняет техническую задачу над repository.

Flow:

1. Пользователь создаёт Workspace.
2. Добавляет repository.
3. Создаёт Task с goal и acceptance criteria.
4. Forge создаёт AgentRun.
5. Agent исследует repository.
6. Agent использует разрешённые tools.
7. Изменения выполняются в sandbox.
8. Forge запускает verification.
9. Если verification failed — агент получает evidence.
10. Agent делает bounded self-correction.
11. Опасные side effects требуют approval.
12. Пользователь видит timeline, tool calls, artifacts, diff, tests, duration и cost.
13. Task завершается только при pass/fail/cancel.

---

## Не входит в core scope первых 12 недель

- универсальный Jarvis;
- voice;
- Android client;
- Home Assistant;
- Data Science;
- сложный RAG;
- vector DB без реальной необходимости;
- Kubernetes;
- swarm/multi-agent architecture;
- Teaching Workspace как отдельный продукт.

Эти темы идут в backlog после v1.

---

# 1. Milestones

## M1 — Agent Backend

**Срок:** конец Week 4

Forge умеет:

- принимать Task;
- хранить state;
- запускать single agent;
- выполнять tools;
- писать audit trail;
- ставить approval gate;
- возвращать verification evidence.

---

## M2 — Reliable Agent Runtime

**Срок:** конец Week 8

Forge умеет:

- исполнять код в sandbox;
- переживать worker/process restart;
- cancel/resume;
- выполнять retries безопасно;
- верифицировать результат;
- делать bounded self-correction;
- запускать eval suite.

---

## M3 — End-to-End Product

**Срок:** конец Week 12

Есть:

- iOS/macOS client;
- realtime task timeline;
- approvals;
- artifacts;
- deployed backend;
- security;
- observability;
- cost tracking;
- CI/CD;
- docs;
- demo scenario.

---

# WEEK 1 — Backend Foundation

**Status:** in progress — реализация и документация приняты; итоговый verification/learning checkpoint не подтверждён  
**Оценка:** 24–28 часов  
**Codex:** LEARNING MODE

## Цель

Создать минимальный backend Forge и понять:

- FastAPI request lifecycle;
- async model;
- typed contracts;
- separation of responsibilities;
- API testing.

## Теория

- Python project/package structure.
- FastAPI.
- Pydantic.
- HTTP semantics.
- async/await.
- dependency injection.
- pytest.
- API layer vs service/domain logic.

## Реализация

Создать:

```text
forge/
└── backend/
```

Минимальная backend structure:

```text
backend/
├── app/
│   ├── api/
│   ├── core/
│   ├── schemas/
│   ├── services/
│   └── main.py
└── tests/
```

Endpoints:

```text
GET  /health
POST /workspaces
GET  /workspaces
POST /tasks
GET  /tasks/{task_id}
```

На Week 1 разрешён in-memory repository.

Добавить:

- typed request/response schemas;
- единый error format;
- structured logging;
- API tests;
- первый ADR.

## Deep Dive -> Python Backend

- typing;
- asyncio;
- FastAPI dependencies;
- Pydantic;
- pytest fixtures.

## Exit Criteria

- backend запускается одной командой;
- endpoints имеют typed contracts;
- tests проходят;
- ошибки возвращаются предсказуемо;
- Евгений объясняет lifecycle одного request;
- создан ADR-001.

---

## Фактический прогресс — 2026-11-08

**Completed (GitHub evidence):**
- Task 01: FastAPI bootstrap, `GET /health`, базовые API tests.
- Task 02: in-memory Workspace API и изоляция `create_app()`.
- Task 03: Task API, validation, проверка существования Workspace; PR #1 merged в main (squash commit `abca7f9`).
- Task 04: единый ErrorResponse, безопасные validation details, сохранение HTTP headers; PR #2 merged (`ebeb2b1`).
- Task 05: структурированные HTTP JSON logs, request ID и middleware; PR #3 merged (`5f08a93`).
- Task 06: техническая архитектура, подробный учебный гид, backlog и ADR-001; PR #4 merged (`89df80a`).

**Architecture decisions:** ADR-001 modular monolith, границы HTTP API / services / schemas / logging; Task ≠ AgentRun; persistence вводится в Week 2. Без переключения стека.

**Technical debt / deferred:** детали в корневом `backlog.md` (BL-01 … BL-06). Отдельно не завершена учебная самопроверка по `LEARNING_GUIDE.md`.

**Codex mode:** LEARNING; выполнение небольшими PR с diff review. Новые возможности закреплены в main; GitHub CI пока отсутствует.

**Verification evidence:** в ранних задачах Codex сообщал `26 passed, 1 warning` (Task 03), но независимый итоговый локальный запуск **всего** pytest после PR #4 в переписке не предъявлен. В GitHub для PR #4 не было CI checks. Не утверждать полный final PASS до фактического результата. Проверка `git diff --check` для финальной рабочей копии тоже требует подтверждения.

**What I can explain:** изучение `docs/LEARNING_GUIDE.md` продолжается. Самостоятельный пересказ request lifecycle / Depends / app.state / exception handlers ещё не принят как exit evidence.

**Hours spent:** не сообщено; не оценивать как факт.

**Next step:** пройти финальный тестовый прогон Week 1 параллельно с началом Week 2; после evidence и объяснения жизненного цикла запроса сменить Week 1 status на `done`. Начало Week 2 согласовано пользователем до закрытия учебного checkpoint; архитектурный долг не переносит неготовый функционал как готовый.

# WEEK 2 — Persistence & Data Model

**Status:** in progress — реализация и PostgreSQL-проверки Step 06 завершены по предоставленному отчёту. Впереди финальный review и merge PR #10.

**Оценка:** 24–28 часов  
**Codex:** LEARNING → ENGINEERING

## Цель

Добавить реальную persistence и определить lifecycle Task/AgentRun.

## Рабочие подшаги (детализация текущего roadmap, не новый scope)

- [x] 01. PostgreSQL + Docker Compose: локальный DB instance, конфигурация, healthcheck, named volume, проверка persistence на уровне БД. Evidence: [PR #5](https://github.com/eva-tarasov/myFirstForge/pull/5), локальная проверка persistence сообщена пользователем; в текущем сеансе не повторялась.
- [x] 02. SQLAlchemy 2.x async: engine/session и typed configuration. Evidence: PR #6 merged в main, squash `8622798`; 57 passed, 2 skipped, 1 warning; 2 PostgreSQL integration tests passed; `git diff --check` проверен пользователем. BL-06 остаётся открытым.
- [x] 03. Initial data model + Alembic. Evidence: PR #7 merged в main, squash `0286c15`; 59 passed, 4 skipped, 1 warning; schema integration 2 passed; Alembic upgrade/current/check/repeated upgrade PASS; `git diff --check` PASS.
- [x] 04. Repository + service boundaries. Evidence: PR #8 merged, squash `24fa648`; 63 passed, 5 skipped, 1 warning; PostgreSQL integration 1 passed; Alembic upgrade/current/check/repeated upgrade и Git diff --check PASS.
- [x] 05. Data contracts для AgentRun, AgentEvent, ToolCall, Approval и Artifact. ORM-модели, revision `ecb758eac83b` и тесты в `main` после merge [PR #9](https://github.com/eva-tarasov/myFirstForge/pull/9), squash `e90ba4c`. Evidence Step 05: `72 passed, 6 skipped, 1 warning`, PostgreSQL integration `1 passed` в прогоне Codex 05B, Alembic upgrade/current/check/repeated upgrade и `git diff --check` — PASS.
- [ ] 06. Persisted state transitions, transaction boundaries и идемпотентное создание Task. `in progress` до завершения review 06D и merge [PR #10](https://github.com/eva-tarasov/myFirstForge/pull/10).
- [ ] 07. Integration/restart/migration verification, documentation, backlog review и Week 2 exit criteria (`not started`).

**Примечание:** это ориентировочная разбивка; количество подшагов может быть скорректировано после review без смены Week 2 scope.

## Фактический прогресс Step 06

- [x] **06A** — доменный граф AgentRun запрещает недопустимые переходы и переоткрытие терминальных состояний.
- [x] **06B1** — `AgentRunService` блокирует строку, сверяет ожидаемый статус и сохраняет новый статус и `AgentEvent` в одной транзакции.
- [x] **06B2** — PostgreSQL-тесты проверяют сохранение переходов и событий, rollback, конкуренцию и отклонение запроса с устаревшим статусом.
- [x] **06C1** — отдельный `TaskStatus`, `tasks.status` с default `queued` и CHECK, revision `2e3bf72ac910`.
- [x] **06C2** — отдельный граф и сохранение переходов Task. `verifying → running` допускается для исправления. PostgreSQL-тесты проверяют блокировку, stale update и rollback.
- [x] **06C3A** — nullable UUID `idempotency_key` и UNIQUE по `(workspace_id, idempotency_key)`, revision `496f3a2c5b81`.
- [x] **06C3B** — `POST /tasks` принимает `Idempotency-Key`, при совпадении данных возвращает исходную Task, при конфликте отвечает `409`. При отмене создания сервис выполняет rollback и пробрасывает `CancelledError`.
- [x] **06C3C** — PostgreSQL-тесты проверяют replay, конкурирующие INSERT с одинаковыми и разными данными, сохранение статуса и освобождение ключа после rollback.

**Архитектурные решения.** ADR-001 остаётся основой. Task и AgentRun имеют отдельные графы. Сервис управляет commit и rollback, а репозиторий выполняет SQL. Блокировка строки и сравнение ожидаемого статуса защищают переходы. Уникальность ключа обеспечивает PostgreSQL, а отдельный SELECT после `ON CONFLICT DO NOTHING` опирается на новый snapshot при `READ COMMITTED`.

**Verification evidence.** По предоставленному локальному отчёту для ревью `51fba9e` в PR #10 обычный pytest дал `241 passed, 31 skipped, 1 warning`, PostgreSQL suite — `25 passed`, в том числе `9 passed` для идемпотентности. Для revision `496f3a2c5b81` проверки Alembic `upgrade`, `current` и `check` прошли. Проверка ограничений идемпотентности в PostgreSQL также прошла. В 06D эти тесты и проверки БД не повторялись.

**Технический долг и ограничения.** Пока нет agent runtime, исполнения инструментов, обработки approvals, оркестрации retry/resume и независимой верификации. Переход в `succeeded` заблокирован до появления evidence. Переходы доступны на уровне сервисов. Нумерация `AgentEvent` через `MAX(sequence) + 1` требует, чтобы все писатели событий удерживали блокировку родительского AgentRun.

**Уроки и темы для объяснения.** Как `FOR UPDATE` и проверка ожидаемого статуса предотвращают stale update. Почему статус и событие должны фиксироваться вместе. Как UNIQUE, `ON CONFLICT` и новый snapshot позволяют распознать replay после конкурентного INSERT. Почему порядок критериев входит в сравнение payload.

**Python Backend Deep Dive.** Границы транзакции в `AsyncSession`, `flush` и `commit`, rollback при `asyncio.CancelledError`, отдельная сессия на конкурентную операцию и синхронизация PostgreSQL-тестов через `asyncio.Event` и `pg_blocking_pids()`.

**Hours spent.** Не сообщено.

**Следующий шаг.** Финальный review 06D и merge PR #10, затем Step 07.

## Теория

- PostgreSQL;
- SQLAlchemy 2.x async;
- transactions;
- Alembic;
- indexes/constraints;
- state machine;
- idempotency basics.

## Data Model v1

```text
Workspace
Task
AgentRun
AgentEvent
ToolCall
Approval
Artifact
```

Task states:

```text
queued
running
waiting_approval
verifying
succeeded
failed
cancelled
```

## Реализация

- PostgreSQL.
- Docker Compose.
- SQLAlchemy models.
- Alembic.
- Repository layer.
- Service/use-case layer.
- Transaction boundaries.
- Integration tests.
- Persisted Task transitions.
- environment configuration.

## Exit Criteria

- restart backend не теряет state;
- migrations поднимают БД с нуля;
- invalid transition запрещён;
- integration tests проходят;
- transaction boundaries понятны.

---

# WEEK 3 — First Single Agent

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** LEARNING MODE

## Цель

Добавить первый настоящий single-agent runtime.

## Теория

- model call vs agent loop;
- instructions;
- structured outputs;
- tool calling;
- context;
- timeout/retry;
- tracing;
- usage/cost;
- durable state vs model context.

## Реализация

Перед подключением основной SDK пройти **Week 3 Technology Decision Gate** из раздела Project Governance; не менять domain model ради фреймворка.

Добавить:

- AgentRunner abstraction;
- AgentService;
- актуальный поддерживаемый OpenAI agent stack;
- AgentRun lifecycle;
- AgentEvent persistence;
- structured final result;
- timeout handling;
- tracing;
- usage/cost capture;
- fake/mock model tests where practical.

Пример final result:

```text
summary
status
assumptions
evidence
blocked_reason
```

## Ограничения

Пока агент:

- не получает unrestricted shell;
- не выполняет произвольные команды на host;
- не получает сложный MCP.

## Exit Criteria

- Task запускает AgentRun;
- AgentRun state сохраняется;
- output структурирован;
- timeout/error корректно отражаются;
- usage/cost видны;
- Евгений объясняет agent loop.

---

# WEEK 4 — Tool System, Permissions & Audit

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** LEARNING -> ENGINEERING

## Цель

Дать агенту контролируемые tools.

## Теория

- tool schemas;
- capability model;
- least privilege;
- path allowlist;
- side effects;
- approvals;
- audit log;
- tool timeout/error contract.

## Tool Contract

```text
name
version
typed input
typed output
risk level
timeout
side_effect
requires_approval
audit metadata
```

## Tools v1

Read-only:

```text
list_files
read_file
search_files
git_diff
```

Controlled write:

```text
apply_patch
```

Запись разрешена только в workspace/fixture root.

## Реализация

- ToolRegistry.
- ToolExecutor.
- PermissionPolicy.
- path traversal protection.
- ToolCall persistence.
- approval records.
- fixture repository.
- tests.

## Verification v1

Проверять:

- ожидаемый файл создан/изменён;
- запрещённые path не затронуты;
- patch соответствует scope.

## Exit Criteria — M1

- Task -> AgentRun -> ToolCall -> Result работает end-to-end;
- агент читает fixture repo;
- controlled write работает;
- approval gate работает;
- audit trail полный;
- path traversal закрыт тестами;
- success требует evidence.

---

# WEEK 5 — MCP

**Status:** not started  
**Оценка:** 22–28 часов  
**Codex:** LEARNING -> ENGINEERING

## Цель

Освоить MCP как внешний tool boundary.

## Теория

- host/client/server;
- tools;
- resources;
- prompts;
- transport;
- discovery;
- auth boundary;
- native tools vs MCP;
- permission implications.

## Реализация

Создать собственный минимальный Forge MCP server.

Tools:

```text
get_repository_metadata
get_test_manifest
get_project_information
```

Затем:

- Forge MCP client;
- dynamic tool discovery;
- mapping MCP metadata -> internal permissions;
- malformed/unavailable MCP tests;
- один внешний MCP integration только если он реально полезен.

## Exit Criteria

- Forge обнаруживает MCP tools;
- MCP tools используют общий audit/permissions;
- failure MCP server не рушит AgentRun;
- создан ADR native tools vs MCP.

---

# WEEK 6 — Sandboxed Execution

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** LEARNING -> ENGINEERING

## Цель

Безопасно позволить агенту запускать команды.

## Теория

- process isolation;
- Docker;
- filesystem mounts;
- network restrictions;
- resource limits;
- timeouts;
- ephemeral environments;
- secrets;
- threat model.

## Execution Abstraction

```text
ExecutionEnvironment
    prepare()
    exec()
    read_artifact()
    cleanup()
```

## Реализация

Первая реализация:

**Docker-based sandbox**

Добавить:

- isolated execution;
- workspace copy/mount;
- run_command только через sandbox;
- timeout;
- CPU/memory limits;
- network disabled by default;
- explicit network capability;
- artifact collection;
- cleanup.

## Verification

Проверить:

- host paths недоступны;
- зависшая команда завершается timeout;
- network block работает;
- artifacts сохраняются.

## Exit Criteria

- агент исправляет простой bug в fixture repo;
- tests запускаются внутри sandbox;
- host не используется для agent shell;
- timeout/cleanup подтверждены;
- security boundary задокументирован.

---

# WEEK 7 — Durable Workflows

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** LEARNING MODE

## Цель

Сделать AgentRun независимым от HTTP connection и process lifetime.

## Теория

- workflow vs activity;
- durable execution;
- deterministic workflow;
- retry;
- idempotency;
- at-least-once;
- cancellation;
- signals;
- human-in-the-loop.

## Реализация

Перед внедрением провести **Week 7 Technology Decision Gate**: Temporal (исходный кандидат) vs LangGraph на сценарии с approval, resume и restart. Если сравнение не выявило причины менять выбор, подключить Temporal. Если выявило — сначала зафиксировать ADR и обновить roadmap.

Подключить Temporal при подтверждении выбора.

Workflow v1:

```text
prepare_workspace
        |
execute_agent
        |
approval?
   /         \
 yes         no
  |           |
wait       verify
  |           |
resume        |
   \         /
     finalize
```

LLM, API и sandbox calls идут через activities.

Добавить:

- retry policy;
- cancellation;
- timeout;
- durable approval wait;
- recovery;
- correlation IDs.

## Chaos Test

```text
start task
kill worker
restart worker
verify workflow continuation
```

## Exit Criteria

- client disconnect не прекращает task;
- worker restart не теряет workflow;
- cancel работает;
- unsafe side effect не дублируется;
- Евгений объясняет deterministic workflow boundary.

---

# WEEK 8 — Verification, Self-Correction & Evals

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** ENGINEERING MODE

## Цель

Создать полноценную verification/eval subsystem.

## Теория

- acceptance criteria;
- deterministic verifier;
- model evaluator;
- false positive/false negative;
- eval dataset;
- regression eval;
- latency/cost/success metrics;
- bounded self-correction.

## Verifier

Code Verifier v1:

```text
build
tests
lint/static checks
forbidden path checks
expected artifact assertions
```

## Self-Correction Loop

```text
execute
  |
verify
  |
FAIL
  |
failure evidence
  |
agent correction
  |
verify
```

Ограничения:

- max attempts;
- token budget;
- cost budget;
- wall-clock timeout.

## Eval Suite

Минимум 15–20 задач:

```text
file edit
bug fix
failing test
small refactor
config change
permission denied
timeout
tool failure
```

Метрики:

```text
success rate
duration
tool calls
retries
tokens
cost
failure category
```

## Важно

Не добавлять reviewer-agent в core flow.

Сначала доказать качество:

```text
single agent
+
good tools
+
verification
```

## Exit Criteria — M2

- eval suite повторяем;
- есть baseline;
- можно сравнить две версии agent runtime;
- correction loop ограничен;
- success требует evidence;
- метрики сохраняются.

---

# WEEK 9 — Native Apple Client

**Status:** not started  
**Оценка:** 24–28 часов  
**Codex:** ENGINEERING MODE

## Цель

Сделать Forge настоящим продуктом.

## Client

Один SwiftUI codebase для:

- macOS;
- iOS;

насколько это практически разумно.

## Screens v1

```text
Workspaces
Tasks
Create Task
Task Detail
AgentRun Timeline
Tool Calls
Artifacts
Approvals
Cancel
```

На Week 9 polling допустим.

Realtime — Week 10.

## Архитектура

- typed API client;
- DTO/domain mapping;
- async/await;
- state ownership;
- error handling;
- secure dev/session token storage.

## Exit Criteria

- основной flow работает из приложения;
- network errors обрабатываются;
- transport model не протекает во весь UI;
- demo больше не требует Postman/curl.

---

# WEEK 10 — Realtime UX

**Status:** not started  
**Оценка:** 24–28 часов  
**Codex:** ENGINEERING -> AGENTIC для знакомых Apple-задач

## Цель

Показывать long-running task progress в реальном времени.

## Решение

По умолчанию:

**SSE для server -> client events**

HTTP для команд:

```text
POST /tasks/{id}/cancel
POST /approvals/{id}/approve
POST /approvals/{id}/reject
```

WebSocket вводить только при реальной двусторонней realtime-потребности.

## Реализация

- persisted event stream;
- SSE endpoint;
- sequence/event IDs;
- reconnect;
- missed-event recovery;
- Swift SSE client;
- live timeline.

Events:

```text
planning
tool_started
tool_completed
verification_started
verification_failed
waiting_approval
completed
failed
```

APNs completion notification — stretch goal.

## Exit Criteria

- UI обновляется realtime;
- reconnect не дублирует events;
- missed events восстанавливаются;
- disconnect клиента не влияет на Task.

---

# WEEK 11 — Production Hardening

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** ENGINEERING MODE

## Цель

Убрать состояние «работает только локально».

## Security

- authentication;
- authorization;
- secret management;
- input validation;
- CORS/network policy;
- permission audit;
- sandbox review.

## Reliability

- timeout/retry review;
- idempotency review;
- graceful failure;
- migrations deployment;
- health/readiness;
- backup/restore plan.

## Observability

- structured logs;
- correlation IDs;
- traces;
- error reporting;
- latency;
- success/failure categories;
- tokens;
- cost;
- budget limits.

## CI/CD

GitHub Actions:

```text
lint
typecheck
unit tests
integration tests
migration check
eval smoke
```

Deploy:

- backend;
- PostgreSQL;
- Temporal worker/runtime.

Redis добавлять только если появилась реальная необходимость.

## Exit Criteria

- demo доступен не только локально;
- secrets не хранятся в repo;
- CI блокирует regression;
- один task traceable end-to-end;
- security checklist заполнен.

---

# WEEK 12 — Portfolio Release

**Status:** not started  
**Оценка:** 24–30 часов  
**Codex:** ENGINEERING + AGENTIC

## Цель

Превратить Forge в доказательство инженерной компетенции.

## Cleanup

- удалить dead code;
- закрыть критический tech debt;
- прогнать tests;
- прогнать full eval suite;
- tag `v1.0`.

## Documentation

README:

- что такое Forge;
- проблема;
- architecture;
- agent lifecycle;
- tools;
- permissions;
- sandbox;
- durable workflow;
- verification;
- evals;
- SSE;
- security;
- observability;
- cost;
- trade-offs;
- local run.

ADR:

- FastAPI;
- PostgreSQL;
- single-agent first;
- MCP;
- sandbox;
- Temporal;
- SSE;
- auth.

## Portfolio Assets

- demo video;
- screenshots iOS/macOS;
- reproducible scenario;
- failure/recovery demo;
- eval report;
- architecture diagram;
- system-design story;
- CV/LinkedIn bullets.

## Final Demo

```text
demo repo
  |
failing test
  |
Forge Task
  |
agent investigates
  |
tools
  |
patch in sandbox
  |
tests
  |
FAIL
  |
failure evidence
  |
self-correction
  |
tests
  |
PASS
  |
timeline + diff + tests + cost
  |
Task succeeded
```

Отдельный reliability demo:

```text
running task
  |
kill worker
  |
restart worker
  |
workflow continues
```

## Exit Criteria — M3

- README позволяет новому человеку запустить demo;
- core flow стабилен;
- full eval baseline сохранён;
- deployed backend есть;
- Apple client работает end-to-end;
- docs завершены;
- Евгений может провести 10–15 минут system design walkthrough без подсказки.

---

# Weekly Report Template

В конце каждой недели:

```text
Week:
Status:

Completed:
-

Architecture decisions:
-

What I can explain now:
-

Bugs / lessons:
-

Technical debt:
-

Deep Dive -> Python Backend:
-

Codex usage:
- Learning:
- Engineering:
- Agentic:

Verification evidence:
-

Hours spent:
-

Next step:
-
```

---

# Quality Gates

Перед `done`:

```text
[ ] код запускается
[ ] tests проходят
[ ] негативные сценарии проверены
[ ] ошибки не проглочены
[ ] logging позволяет расследовать failure
[ ] architecture decisions зафиксированы
[ ] существенный Codex-generated code просмотрен
[ ] Евгений может объяснить реализацию
[ ] нет технологий, добавленных только ради CV
```

---

# Backlog after Week 12

Только после M3:

## Week 13+

- multi-agent: Planner / Executor / Reviewer;
- voice;
- Teaching Workspace dogfooding;
- GitHub deeper integration;
- Gmail;
- Calendar;
- Drive;
- Home Assistant;
- RAG/hybrid retrieval — только при реальной необходимости;
- team collaboration;
- billing;
- multi-tenancy;
- hosted sandboxes.

