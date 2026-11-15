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

- **Week 3 — Agent Runtime:** владелец утвердил LangChain + LangGraph как образовательное и архитектурное направление ([ADR-002](docs/adr/ADR-002.md)). Один основной production-minded runtime — собственный `StateGraph` за независимым Forge `AgentRunner`. `LangChain create_agent` — ограниченный учебный прототип, прямой loop — mental model, OpenAI Agents SDK отложен. Step 10 проверил tool calls, structured output, instrumentation, cost и persistence в offline flow с SDK и PostgreSQL. Live provider, production-пригодность и approvals integration остаются открытыми.

  Для Week 3 владелец явно утвердил LangChain и LangGraph по образовательным причинам, изложенным в ADR-002. Это решение относится к текущему milestone и не изменяет общие критерии Architecture Change Gate. Production-пригодность выбранной реализации должна быть подтверждена последующими проверками.
- **Week 7 — Durable Orchestration:** исходная кандидатура — **Temporal**. Сравнить с **LangGraph** на одинаковом сценарии `execute → approval pause → resume → verification → worker restart`. Проверить recovery semantics, безопасные side effects, checkpoint/replay, сложность deployment и соответствие нашему domain state. При необходимости возможно сочетание разных уровней, но **не подключать оба по умолчанию**.
- **LangSmith / RAG / multi-agent / Deep Agents / Redis / другие фреймворки:** не добавлять без конкретной потребности текущего milestone и прохождения Architecture Change Gate.
- Изменение реализации не должно уничтожать domain model и boundaries без отдельного ADR.

## 4. Weekly execution and status

- Для каждой недели: `design/theory → one scoped implementation task → tests/verification → diff review → explanation → progress update`.
- Codex выполняет routine unit/regression tests, dependency checks и обычные integration checks текущего scope. Владелец вручную запускает выбранные преподавателем критические учебные сценарии и существенные проверки lifecycle/security/failure modes. Результаты всех проверок фиксируются как evidence. Не требовать повторного ручного прогона всех routine tests без необходимости.
- Статусы `not started / in progress / blocked / done` менять **по фактическому evidence**, не по календарю и не по отчёту агента без проверки.
- Если встречена интересная новая технология — внести её в **Decisions to revisit**, не прерывая текущую неделю.
- При отклонении от roadmap указывать: `why`, `trade-off`, `ADR`, `migration cost`, `effect on exit criteria`.
- В архитектурном чате `00` сначала теория на английском, затем тот же блок по-русски.

## Decisions to revisit

| Decision | Planned gate | Current stance | Required evidence |
|---|---|---|---|
| Week 3 Agent Runtime | Week 3 | LangChain + собственный LangGraph StateGraph утверждены для обучения и реализации; [ADR-002](docs/adr/ADR-002.md). OpenAI Agents SDK отложен | contract, state mapping, safe tool, structured output, usage/cost, error and cancellation tests; production-пригодность ещё не доказана |
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

**Status:** done — инженерная реализация и административное закрытие Week 2 завершены: [PR #11](https://github.com/eva-tarasov/myFirstForge/pull/11) объединён в `main` (squash `af98e57`). Отдельный learning checkpoint 07C3 остаётся незавершённым и отложен по решению владельца.

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
- [x] 06. Persisted state transitions, transaction boundaries и идемпотентное создание Task. Review и проверки завершены, [PR #10](https://github.com/eva-tarasov/myFirstForge/pull/10) объединён с `main` (squash `571f228`).
- [x] 07. Integration/restart/migration verification, documentation, backlog review и инженерные Week 2 exit criteria. Технические проверки завершены по приведённому ниже историческому evidence; документация и обновление прогресса вошли в `main` через [PR #11](https://github.com/eva-tarasov/myFirstForge/pull/11). Отдельный learning checkpoint 07C3 открыт.
  - [x] **07A** — пользователь прочитал сохранённую Task через HTTP до и после перезапуска backend. Ответы совпали.
  - [x] **07B** — пользователь применил цепочку миграций к чистой PostgreSQL-базе. Достигнут `head`, `alembic check` и повторный upgrade прошли.
  - [x] **07C1** — [учебное руководство Week 2](docs/LEARNING_GUIDE_WEEK2.md) прошло содержательный review и вошло в `main` через PR #11 (squash `af98e57`).
  - [x] **07C2** — финальное обновление прогресса и backlog вошло в `main` через PR #11 (squash `af98e57`); файлы присутствуют в составе коммита.
  - [ ] **07C3** — вопросы и опорные ответы предоставлены. Самостоятельная устная и практическая проверка отложена по просьбе пользователя. Результат оценки отсутствует.

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

## Итоговый отчёт Week 2

**Status.** `done` для инженерной части Week 2: PR #11 merged в `main` (`af98e57`), технические exit checks завершены по историческим локальным результатам пользователя. Самостоятельная оценка знаний 07C3 остаётся открытым learning checkpoint.

**Completed.** PR #5–10 добавили PostgreSQL и Compose, async SQLAlchemy и Alembic, persistent Workspace и Task, модели execution, переходы Task и AgentRun, атомарное сохранение статуса AgentRun и события, идемпотентное создание Task. Проверки перезапуска и миграций с нуля завершены. Гайд Week 2 и обновление progress/backlog вошли в `main` через PR #11.

**Architecture decisions.** ADR-001 и modular monolith остаются основой. Task и AgentRun имеют отдельные графы. Сервис управляет commit и rollback, репозиторий выполняет SQL. `FOR UPDATE` и проверка ожидаемого статуса защищают переходы. PostgreSQL обеспечивает уникальность пары Workspace и UUID-ключа, а отдельный SELECT после `ON CONFLICT DO NOTHING` опирается на новый snapshot при `READ COMMITTED`.

**Topics covered.** Путь запроса от FastAPI до PostgreSQL, маппинг ORM и домена, сохранение состояния после перезапуска, независимые графы переходов, блокировки, идемпотентность и сценарии отказа. В гайде есть вопросы и опорные ответы. Пользователь отложил самостоятельный пересказ и практическую оценку, поэтому личное освоение тем ещё не подтверждено.

**Bugs / lessons.** Review 06C3B выявил, что `except Exception` пропускал `asyncio.CancelledError` при создании Task. Исправление добавило rollback с повторным выбросом исключения и unit-тест (`bc5ebbe`). Конкурентные проверки наблюдают реальное ожидание через `pg_blocking_pids()` и читают сохранённое состояние из отдельной сессии.

**Technical debt.** В [backlog](backlog.md) остаются открытыми BL-05 (GitHub CI) и BL-06 (предупреждение Starlette TestClient). Отложены вопросы неизменяемой идентичности идемпотентного запроса, сериализации записи событий и смены уровня изоляции (BL-07–09), а также самостоятельная учебная оценка. Agent runtime, исполнение инструментов, sandbox, обработка approvals, durable recovery и независимая верификация относятся к будущим этапам. Сервисы пока отклоняют переход в `succeeded`.

**Deep Dive → Python Backend.** Lifetime `AsyncSession`, `flush` и `commit`, rollback при отмене, отдельная сессия на конкурентную операцию, координация тестов через `asyncio.Event` и видимость строк при `READ COMMITTED`.

**Codex usage.** LEARNING использовался при освоении persistence и модели транзакций. ENGINEERING применялся для интеграционных проверок, документации и итогового review прогресса.

**Verification evidence.** Ниже исторические локальные результаты, сообщённые пользователем. Эти проверки не являются результатами GitHub CI и не повторялись Codex в 07C2.

| Проверка | Результат |
| --- | --- |
| Default pytest | `241 passed, 31 skipped, 1 warning` |
| PostgreSQL Step 06, три интеграционных модуля | `25 passed`, из них `9 passed` для идемпотентности Task. Для ревью `51fba9e` также зафиксирован PASS проверки ограничений идемпотентности и Alembic `upgrade`, `current`, `check` |
| 07A, restart backend | HTTP-ответ для сохранённой Task совпал до и после перезапуска |
| 07B, чистая `forge_task07_20261111_164823` | Upgrade до `496f3a2c5b81 (head)`, `alembic check` → `No new upgrade operations detected`, повторный `upgrade head` — PASS |

**Hours spent.** Не сообщено.

**Next step.** Review [ADR-002](docs/adr/ADR-002.md) и обновлённого progress для Week 3 Step 01; после согласования — Step 02 LangChain Model Foundation. Учебная оценка 07C3 остаётся отложенной.

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

**Status:** done — техническая реализация и verification приняты владельцем. Самостоятельный учебный checkpoint отложен и остаётся неподтверждённым.

**Оценка:** 29–39 часов из-за дополнительного изучения LangChain и LangGraph; фактические часы не сообщены

**Codex:** LEARNING MODE

## Цель

Добавить первый настоящий single-agent runtime.

## Рабочие шаги

- [x] 01. Architecture Decision & Theory — DONE. [ADR-002](docs/adr/ADR-002.md) принят владельцем; commits `af26d519` и `9ab4b07` проверены.
- [x] 02. LangChain Model Foundation — DONE.
  - [x] 02A. Model foundation — DONE. [PR #12](https://github.com/eva-tarasov/myFirstForge/pull/12) merged в `main` (`9efde8f`). Историческое evidence: `pip check` PASS, model tests `18 passed`, полный pytest `259 passed, 31 skipped`. В отдельном прогоне владелец сообщил `2 passed` для PostgreSQL connection smoke, остальные 29 PostgreSQL integration tests не выполнялись.
  - [x] 02B. Structured output — DONE. [PR #13](https://github.com/eva-tarasov/myFirstForge/pull/13) merged в `main` (`8830d5e`). Историческое evidence: `pip check` PASS, model tests `43 passed`, полный pytest `284 passed, 31 skipped`, пять критических ручных учебных проверок PASS по отчёту владельца. PostgreSQL integration tests не запускались.
- [x] 03. Tool Calling & LangChain create_agent — DONE.
  - [x] 03A. Tool Calling Foundation — DONE. [PR #14](https://github.com/eva-tarasov/myFirstForge/pull/14) merged в `main` (`4d80293`). Историческое evidence: `pip check` PASS, model tests `64 passed`, полный pytest `305 passed, 31 skipped`, четыре ручных учебных случая PASS по отчёту владельца. PostgreSQL integration tests не запускались.
  - [x] 03B. LangChain create_agent — DONE. [PR #15](https://github.com/eva-tarasov/myFirstForge/pull/15) merged в `main` (`e2bad56`). Историческое evidence: `pip check` PASS, сохранены версии всех 57 ранее установленных non-editable пакетов, model/agent tests `80 passed`, полный pytest `321 passed, 31 skipped`, четыре ручных учебных случая PASS по отчёту владельца. PostgreSQL connection smoke NOT RUN: сервис недоступен. Dev dependency: `langchain==1.4.10`. В 03B транзитивно установлены `langgraph==1.2.21`, `langgraph-checkpoint==4.3.1`, `langgraph-prebuilt==1.1.1`, `langgraph-sdk==0.4.14` и `ormsgpack==1.13.1`.
- [x] 04. LangGraph Fundamentals — DONE.
  - [x] 04A. StateGraph Foundation — DONE. [PR #16](https://github.com/eva-tarasov/myFirstForge/pull/16) merged в `main` (`dec5580`). Историческое evidence: Python `3.13.0`, `pip check` PASS, graph tests `9 passed`, полный pytest `330 passed, 31 skipped`, четыре ручных учебных случая PASS по отчёту владельца. `langgraph==1.2.21` объявлен прямой зависимостью, версии всех 63 non-editable пакетов сохранены. PostgreSQL integration tests не запускались.
  - [x] 04B. Reducers, Messages & Cyclic Execution — DONE. [PR #17](https://github.com/eva-tarasov/myFirstForge/pull/17) merged в `main` (`6a44ff0`). Историческое evidence: `pip check` PASS, graph cycle tests `7 passed`, полный pytest `337 passed, 31 skipped`, три ручных учебных случая PASS по отчёту владельца. Версии зависимостей сохранены. PostgreSQL integration NOT RUN.
- [x] 05. Custom Single-Agent Graph — DONE.
  - [x] 05A. Custom Model/Tool Graph — DONE. [PR #18](https://github.com/eva-tarasov/myFirstForge/pull/18) merged в `main` (`2aef2db`). Историческое evidence: `pip check` PASS, graph tests `34 passed`, предыдущие model/tool demo tests `69 passed`, полный pytest `371 passed, 31 skipped`, три начальных ручных учебных сценария PASS и проверка исправления `6 passed, 5 deselected` по отчёту владельца. PostgreSQL integration NOT RUN, версии зависимостей сохранены. Граф использует общий `read_fixture`, проверяет бюджеты перед исполнением и добавляет сообщения с уникальными ID. Checkpointer отключён.
  - [x] 05B. Structured Finalization — DONE. [PR #19](https://github.com/eva-tarasov/myFirstForge/pull/19) merged в `main` (`92a4f85`). Историческое evidence: `pip check` PASS, graph/finalization tests `69 passed` (сохранены все 34 прежних сценария графа), model/settings tests `64 passed`, полный pytest `406 passed, 31 skipped`, четыре ручных учебных случая PASS по отчёту владельца. PostgreSQL integration NOT RUN, версии зависимостей сохранены. Узел `finalize` получает `TaskAnalysis` и формирует `AgentFinalResult` с runtime observations и статусом `ready_for_verification` или `blocked`. Observations содержат фактические синтетические ответы fixture. Независимая verification остаётся отдельным этапом. Бюджет: до двух исследовательских вызовов модели, одно исполнение fixture и один вызов финализатора.
- [x] 06. AgentRunner & AgentService — DONE.
  - [x] 06A. AgentRunner Contract & LangGraph Adapter — DONE. [PR #20](https://github.com/eva-tarasov/myFirstForge/pull/20) merged в `main` (`94da6c5`). Исторические проверки: `pip check` PASS, runner tests `23 passed`, graph/finalization tests `69 passed`, полный pytest `429 passed, 31 skipped`, четыре ручных учебных случая PASS по отчёту владельца. PostgreSQL integration NOT RUN. Зависимости сохранены. Application layer владеет `AgentRunner` Protocol, `AgentRunRequest` и result models. Адаптер возвращает `AgentFinalResult`, оборачивает обычные ошибки исполнения в `AgentRunnerError("execution_failed")` с исходной причиной и пробрасывает asyncio cancellation.
  - [x] 06B. AgentService & Lifecycle Integration — DONE. [PR #21](https://github.com/eva-tarasov/myFirstForge/pull/21) merged в `main` (`4b58198`).
    - Claim блокирует строку `AgentRun`, снимает неизменяемый snapshot задачи и фиксирует `queued → running` вместе с `agent_run_status_changed`. Сессия claim закрывается до запуска `AgentRunner`. Финальный переход открывает отдельную сессию и требует `running`. Если за время исполнения сохранилась отмена, финальный переход отклоняется. `ready_for_verification` переводит run в `verifying`, а `blocked` и ошибки исполнения переводят его в `failed`, пока run находится в `running`.
    - `Task.status` и номер попытки остаются прежними. Сохранение результатов и восстановление после сбоя отложены. Coroutine cancellation пробрасывается и может оставить `AgentRun` в `running`, если отдельного доменного перехода в `cancelled` не было. Это ограничение относится к Step 08 и Week 7.
    - Исторические проверки 06B: `pip check` PASS, обычный pytest `429 passed, 52 skipped`, runner/graph/finalization `92 passed`, пять ручных учебных случаев PASS по отчёту владельца. PostgreSQL suite дал `46 passed` (21 новый и 25 прежних) в базе `forge_task06_week3_20261114_113611`, revision `496f3a2c5b81`, `alembic check` PASS. В прогоне проверены реальный конфликт `SELECT FOR UPDATE`, rollback статуса и события, отмена во время исполнения и полный offline StateGraph с `read_fixture` через `LangGraphAgentRunner`.
- [x] 07. Persistence & Agent Events — DONE.
  - [x] 07A. Safe AgentEvent Append — DONE. [PR #22](https://github.com/eva-tarasov/myFirstForge/pull/22) merged в `main` (`2aaeae5`).
    - AgentEvent получает sequence под блокировкой родительского AgentRun. Репозиторий отключает autoflush при захвате блокировки, явно сбрасывает pending writes и читает `MAX(sequence)` отдельным запросом при `READ COMMITTED`. Status writer использует тот же контракт. Caller владеет транзакцией.
    - PostgreSQL suite дал `61 passed` (15 новых и 46 прежних) в `forge_task06_week3_20261114_113611`, revision `496f3a2c5b81 (head)`, `alembic check` PASS. Проверены блокировки двух generic writers, оба порядка generic/status, rollback с повторным использованием sequence и работа без autoflush.
    - Первый обычный прогон дал `428 passed, 1 failed, 67 skipped`. Заглушка session в `test_event_repository_sequences_and_does_not_commit` не поддерживала `no_autoflush`, `flush` и отдельные результаты двух SQL-запросов. Исправленный тест проверяет блокировку внутри `no_autoflush`, затем `flush → MAX → add`, а также отсутствие commit и rollback со стороны репозитория.
    - После correction в автоматических прогонах Codex прошли отдельный unit-тест (`1 passed`), весь модуль (`14 passed`), обычная регрессия (`429 passed, 67 skipped`) и повторный PostgreSQL suite (`61 passed`, `READ COMMITTED`). Во всех четырёх прогонах 0 failures и 0 pytest warnings. `pip check` PASS.
    - Три ручных учебных сценария PASS по данным владельца: concurrent append, append с выключенным autoflush и rollback. BL-08 закрыт после merge PR #22.
  - [x] 07B. Durable Result Persistence — DONE. [PR #23](https://github.com/eva-tarasov/myFirstForge/pull/23) merged в `main` (`03607dc`).
    - `AgentRun.final_result` хранит nullable JSONB, storage version 1. Completion фиксирует результат, изменение статуса и два события одной транзакцией. Новый переход в `verifying` требует валидного `ready_for_verification`. Исторический `verifying` с SQL NULL сохраняется и читается без backfill. Сохранённый предварительный результат доступен из новой сессии без запуска runner. Для `succeeded` по-прежнему нужна независимая verification.
    - Миграция `1d972cc34fbe (head)` прошла с legacy-строками в `forge_task06_week3_result_20261114_123241` и с нуля в `forge_task06_week3_result_fresh_20261114_123241`. `alembic check` PASS в обеих базах. В legacy-проверке прежние строки и история совпали до и после upgrade, у обоих run SQL NULL.
    - Исходная проверка 07B дала `51 passed` для codec/model suite и `87 passed` для PostgreSQL suite. Обычный pytest дал `466 passed, 5 failed, 93 skipped`, 0 warnings. Все пять сбоев в `test_agent_run_service.py` вызвала устаревшая заглушка AgentRun без `id`.
    - Исправленная заглушка содержит `id`, `status` и `final_result=None`. Unit-тест проверяет, что блокировка строки и событие используют один ID. Отдельная проверка требует `populate_existing=True` у `SELECT FOR UPDATE`, чтобы после ожидания блокировки SQLAlchemy обновляла ранее загруженный AgentRun актуальными данными и второй completion отклонялся по сохранённому статусу.
    - Повторные автоматические проверки дали `14 passed` в модуле AgentRunService, `471 passed, 93 skipped` в обычной регрессии и `87 passed` в PostgreSQL suite (26 новых и 61 прежний). Во всех трёх прогонах 0 failures и 0 pytest warnings. `pip check` PASS. PostgreSQL подтвердил базу `forge_task06_week3_result_20261114_123241`, `READ COMMITTED` и revision `1d972cc34fbe`.
    - Пять ручных учебных сценариев PASS по логам владельца: чтение результата через новый engine, rollback при сбое result event, rollback при сбое status event, отклонение позднего ready и отклонение позднего blocked.
- [x] 08. Reliability & Execution Limits — DONE.
  - [x] 08A. Agent Execution Deadline — DONE. [PR #24](https://github.com/eva-tarasov/myFirstForge/pull/24) merged в `main` (`e5fde60`).
    - `AgentService` применяет один монотонный deadline ко всему `AgentRunner.run()`, по умолчанию 60 секунд. Лимит задаётся keyword-only параметром конструктора. Claim, валидация результата и запись в PostgreSQL выполняются вне этого deadline. Внутренний timeout runner и существующие ошибки провайдера сохраняют исходный тип и причину.
    - Истечение deadline даёт `execution_deadline_exceeded`. Текущий `running` переходит в `failed` с SQL NULL. Уже сохранённый `cancelled` сохраняется. Поздний результат отклоняется, даже если runner перехватил отмену или event loop ещё не запустил callback таймера. Внешняя отмена coroutine пробрасывается и может оставить run в `running`. Отмена кооперативная. Автоматических повторов и восстановления исполнения нет.
    - `pip check` PASS. Deadline unit suite `23 passed`, runner/graph/finalization `92 passed`, обычный pytest `494 passed, 96 skipped`. PostgreSQL suite `90 passed` (3 новых и 87 прежних) в `forge_task06_week3_result_20261114_123241` при `READ COMMITTED`, revision `1d972cc34fbe`, `alembic check` PASS. Подтверждены отмена research и финализации в реальном StateGraph и сохранение committed cancellation. Во всех прогонах 0 failures и 0 pytest warnings.
    - Пять ручных учебных сценариев PASS по отчёту владельца. `git diff --check` PASS.
  - [x] 08B. Error Classification, Failure Metadata & Retry Policy — DONE. [PR #25](https://github.com/eva-tarasov/myFirstForge/pull/25) merged в `main` (`fd52c07`).
    - Application layer владеет 16 стабильными кодами и вычисляет `transient_candidate`, `not_retryable` или `review_required`. Runtime adapter классифицирует исключения SDK, графа и structured output. Общий deadline остаётся отдельным кодом. Автоматические повторы, новая попытка и durable recovery за пределами 08B.
    - `agent_execution_failed` содержит только `schema_version` и `code`. Одна транзакция сохраняет его и переход `running → failed`, оставляя SQL NULL в `final_result`. `get_run_failure()` читает событие без исполнения runner. Исторические строки без metadata и сохранённые `blocked` results возвращают `None`.
    - `pip check` PASS. Failure contract/classification `69 passed`, runner/deadline/graph/finalization `115 passed`, обычный pytest `563 passed, 113 skipped`. PostgreSQL suite `107 passed` (17 новых и 90 прежних) в `forge_task06_week3_result_20261114_123241`, `READ COMMITTED`, revision `1d972cc34fbe`, `alembic check` PASS. Проверены все пять HTTP MockTransport сценариев, чтение через новый engine, rollback обоих событий, конкурентная запись и сохранение доменной отмены. Во всех итоговых прогонах 0 failures и 0 pytest warnings.
    - Восемь ручных учебных сценариев PASS по отчёту владельца. `git diff --check` PASS.
  - [x] 08C. Cancellation Management — DONE. [PR #26](https://github.com/eva-tarasov/myFirstForge/pull/26) merged в `main` (`5c1174b`).
    - `cancel_execution()` сохраняет `cancelled` и событие смены статуса под блокировкой AgentRun. Повтор идемпотентен. `cancel_run()` сигнализирует локальному реестру после подтверждённого commit и выхода из сессии. Реестр регистрирует каждую попытку до claim и вызывает `Task.cancel()` только во время runner. Cleanup handle выполняется в `finally`. Реестр можно явно разделить между сервисами одного процесса и event loop.
    - Проверены rollback отмены без сигнала, отмена research и финализации реального StateGraph, обе стороны гонок с claim и записью исхода, сохранение результата при отмене из `verifying` и сохранение metadata, если ошибка записалась первой. Coroutine cancellation отдельно от доменной отмены. Отмена кооперативна. Завершение cleanup, отзыв внешнего действия и distributed recovery остаются за пределами гарантии.
    - `pip check` PASS. Cancellation unit suite `22 passed`, связанные unit/regression тесты `129 passed`, обычный pytest `585 passed, 131 skipped`. PostgreSQL suite `125 passed` (18 новых и 107 прежних) в `forge_task06_week3_result_20261114_123241`, `READ COMMITTED`, revision `1d972cc34fbe`, `alembic check` PASS. Во всех прогонах 0 failures и 0 pytest warnings.
    - Пять ручных учебных сценариев PASS по отчёту владельца. `git diff --check` PASS.
- [x] 09. Usage, Cost & Observability — DONE.
  - [x] 09A. Model Usage Capture — DONE. [PR #27](https://github.com/eva-tarasov/myFirstForge/pull/27) merged в `main` (`8a07636`).
    - Явный `capture_agent_usage(run_id)` передаёт изолированный collector через `ContextVar`. Research и structured finalization учитываются на границе `ainvoke()` до валидации ответа. Snapshot сохраняет наблюдавшийся usage после ошибки графа, deadline или отмены. `reported`, `missing` и `invalid` различаются. Known totals суммируют валидные основные счётчики. Cost, PostgreSQL persistence и автоматическое владение scope остаются последующим этапам.
    - MockTransport подтвердил три вызова и 100 токенов (71 input, 29 output). Параллельные исполнения одного StateGraph подтвердили изоляцию scopes. Usage suite `64 passed`, связанные regression tests `168 passed`, обычный pytest `649 passed, 131 skipped`, PostgreSQL suite `125 passed` в `forge_task06_week3_result_20261114_123241` при `READ COMMITTED`, revision `1d972cc34fbe`. `pip check` и `alembic check` PASS. Во всех прогонах 0 failures и 0 pytest warnings.
    - Девять ручных учебных сценариев PASS по отчёту владельца. `git diff --check` PASS.
  - [x] 09B. Cost Estimation & Durable Usage Persistence — DONE. [PR #28](https://github.com/eva-tarasov/myFirstForge/pull/28) merged в `main` (`2266183`).
    - `AgentService` владеет usage scope только во время runner после успешного claim. Непустой snapshot и исторические тарифы хранятся одним versioned `agent_usage_recorded` в отдельной транзакции после lifecycle. Повтор идентичной записи идемпотентен. Чтение через новый engine воспроизводит `0.00034250 USD` по синтетическому тарифу, независимо от текущей конфигурации.
    - Usage сохраняется при ready, blocked, ошибках парсинга и финализации, deadline и отмене. При сбое записи usage lifecycle остаётся закоммиченным. Первичная ошибка сохраняет identity и cause, получая стабильную заметку. Crash и повторная внешняя отмена до commit могут оставить usage незаписанным. Оценка не сверяется с биллингом.
    - Python Deep Dives. Явный `Decimal.Context` сохраняет точность цен независимо от ambient context. Десятичные строки в JSONB сохраняют ставку и её scale. `ContextVar` восстанавливает внешний collector. `finally` выполняет отдельную запись usage и очистку handle, а `BaseException.add_note()` сохраняет первичный `CancelledError` при вторичной ошибке записи.
    - Cost/usage suite `144 passed`, связанные регрессии `137 passed`, обычный pytest `729 passed, 152 skipped`, PostgreSQL suite `146 passed` (21 новый и 125 прежних). В базе `forge_task06_week3_result_20261114_123241` подтверждены `READ COMMITTED`, revision `1d972cc34fbe` и `alembic check` PASS. Проверены rollback usage, запись после rollback completion и конкуренция с `pg_blocking_pids()`. `pip check` PASS. Во всех прогонах 0 failures и 0 pytest warnings.
    - Пять ручных учебных сценариев PASS по отчёту владельца. `git diff --check` PASS.
  - [x] 09C. Observability — DONE. [PR #29](https://github.com/eva-tarasov/myFirstForge/pull/29) merged в `main` (`9edbf5e`).
    - `forge.agent` пишет безопасные JSON-события выполнения фаз и операций, классифицированные сбои и агрегаты наблюдаемого usage. `operation_id` выделяется для каждого вызова, `run_id` связывает исполнение и отмену, `task_id` берётся из успешного claim. PostgreSQL сохраняет историю AgentEvent по существующим правилам.
    - Python `3.13.0`, `pip check` PASS. Observability/HTTP suite `26 passed` (20 новых и 6 прежних), связанные регрессии `164 passed`, обычный pytest `749 passed, 156 skipped` (PostgreSQL opt-in выключены). PostgreSQL suite `150 passed` (4 новых и 146 прежних), база `forge_task06_week3_result_20261114_123241`, `READ COMMITTED`, revision `1d972cc34fbe`, `alembic check` PASS. Во всех итоговых pytest-прогонах 0 failures и 0 warnings.
    - В реальном StateGraph с синтетическими ответами и `read_fixture` проверены ready, deadline, явная отмена и rollback записи usage после PostgreSQL CHECK. Проверки подтверждают прежние последовательности AgentEvent и раздельные транзакции. Тесты отказа sink проверяют отсутствие утечек в JSON и stderr и сохранение исходных ошибок и отмены.
    - 11 ручных учебных сценариев PASS по отчёту владельца. BL-04 закрыт для событий агента. BL-02 и BL-03 остаются открыты.
    - Python Deep Dives. `Handler` отвечает за запись в поток, а `Formatter` проверяет и сериализует поля. Синхронный context manager замеряет фазу вокруг `await` и пробрасывает исходное исключение. `perf_counter_ns()` измеряет длительность, а UTC timestamp задаёт время записи. Локальный контекст корреляции сохраняется при чередовании asyncio-задач.
- [x] 10. Integration & Week 3 Review — DONE. Технический review принят. [PR #30](https://github.com/eva-tarasov/myFirstForge/pull/30) merged в `main` (squash `d2534379de9d8c25036f0ecbdef46197992b5041`).
  - [x] 10A. Architecture audit & Exit criteria — DONE.
    - Read-only architecture audit сверил modular monolith, `AgentRunner` и границы PostgreSQL, явный claim до runner, lifecycle guards, fixture-only graph, бюджеты и отмену. [Матрица из 15 критериев](docs/LEARNING_GUIDE_WEEK3.md#матрица-architecture-audit).
  - [x] 10B. Full integration verification — DONE.
    - На свежем head до исправления Task schema дал `1 passed, 1 failed` из-за устаревшего списка колонок. Execution schema дал `1 failed` на nullability. Обновлены проверки точного Task metadata, default `queued`, именованных CHECK и scoped UNIQUE. Execution schema с savepoint проверяет nullable JSONB result, SQL NULL и запрет JSON array/string/null. Исходная схема проверена всей цепочкой миграций.
    - Offline E2E создаёт Task обычным `TaskService`, явно добавляет queued AgentRun, выполняет собственный StateGraph через `ChatOpenAI` и HTTPX MockTransport. Три model requests и один `read_fixture` дают preliminary result и `verifying`. Проверены четыре durable события, 71 input / 29 output / 100 total, 21 cached и 14 reasoning, полные usage/cost и стоимость `Decimal("0.00034250")`. Fresh engine читает исторические записи. Переход в `succeeded` без verification evidence отклонён.
    - Итоговое evidence PR #30. Python `3.13.0`. Обычный pytest дал `749 passed, 157 skipped` при выключенных PostgreSQL opt-ins. Отдельно PostgreSQL дал `157 passed`: 151 основной случай (150 предыдущих и новый E2E) и 6 дополнительных (connection — 2, Task schema — 2, HTTP/repository — 1, execution schema — 1). Всего **906 уникальных тестов прошли**, 0 итоговых failures, 0 pytest warnings, 0 непокрытых skips. `pip check`, `alembic check` и `git diff --check` — PASS. BL-06 в текущем окружении не воспроизведён.
    - `forge_task06_week3_result_20261114_123241` использована для lifecycle и connection. Новые базы `forge_task03_week3_final_20261115_161856`, `forge_task04_week3_final_20261115_161856` и `forge_task05_week3_final_20261115_161856` мигрированы с нуля. Для всех четырёх выделенных баз подтверждены имя, PostgreSQL `17.11`, `READ COMMITTED`, revision `1d972cc34fbe (head)` и `alembic check` PASS. Базы сохранены.
    - По логам ручных прогонов владельца `test_week3_task_to_agent_run_end_to_end`, `test_migrated_schema_metadata` и `test_execution_schema_and_constraints` — PASS.
  - [x] 10C. Learning documentation & milestone review — DONE. Учебные материалы и технический итог недели приняты. [Учебное руководство Week 3](docs/LEARNING_GUIDE_WEEK3.md).
    - Python Deep Dives. `Protocol` изолирует adapter и облегчает fake, `async with` даёт сессии разные сроки жизни, nested transaction создаёт savepoint для ожидаемого SQL error. MockTransport перехватывает SDK-границу до сети. `ContextVar`, frozen snapshot, `Decimal` и отдельный monotonic clock связывают модельные вызовы с проверяемой историей и диагностикой.

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

**Week 3 Technology Decision Gate** пройден решением владельца. Выбраны LangChain и собственный LangGraph `StateGraph` ([ADR-002](docs/adr/ADR-002.md)). Step 10 подтвердил runtime в offline flow с PostgreSQL, реальным SDK и синтетическими ответами MockTransport. Live provider и production-пригодность требуют отдельных проверок. Доменная модель остаётся независимой от framework.

Добавить:

- AgentRunner abstraction;
- AgentService;
- LangChain model abstractions и собственный LangGraph `StateGraph` за контрактом `AgentRunner`; OpenAI — первый предполагаемый model provider;
- AgentRun lifecycle;
- AgentEvent persistence;
- structured final result;
- timeout handling;
- tracing;
- usage/cost capture;
- fake/mock model tests where practical.

В Week 3 допустим только безопасный fixture/read-only tool; полноценный Tool System относится к Week 4. Успешное исполнение графа переводит `AgentRun` лишь к `verifying`; `succeeded` требует независимого verification evidence.

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

### Итоговая приёмка Week 3

| Исходный критерий | Источник и проверка | Итог и открытая граница |
|---|---|---|
| Task запускает AgentRun | [Task API](backend/app/api/tasks.py), [AgentService](backend/app/services/agent_service.py), [E2E](backend/tests/test_agent_service_integration.py) | Владелец принял внутренний flow как достаточный для Week 3. После `TaskService.create_task()` тест явно создаёт queued AgentRun и передаёт его `run_id` в `AgentService.execute_run()`. `POST /tasks` сохраняет только Task. Публичная orchestration остаётся будущей задачей. |
| AgentRun state сохраняется | [AgentRunService](backend/app/services/agent_runs.py), [переходы](backend/tests/test_agent_run_integration.py), [E2E](backend/tests/test_agent_service_integration.py) | Подтверждены durable `verifying`, точная история, fresh-engine read и отказ `succeeded` без evidence. Независимый verifier и durable resume остаются открытыми. |
| output структурирован | [Finalizer](backend/app/agent_runtime/finalization.py), [codec](backend/app/services/agent_result_codec.py), [тесты результата](backend/tests/test_agent_result_persistence_integration.py) | Подтверждены предварительный типизированный результат, runtime observations и JSONB envelope. Наблюдения относятся к synthetic fixture. |
| timeout/error корректно отражаются | [AgentService](backend/app/services/agent_service.py), [классификация](backend/app/services/agent_failure.py), [PG lifecycle suite](backend/tests/test_agent_service_integration.py) | Подтверждены deadline, failure metadata, отмена и rollback. Локальный сигнал кооперативен и действует в пределах процесса. |
| usage/cost видны | [Стоимость](backend/app/services/agent_cost.py), [usage codec](backend/app/services/agent_usage_codec.py), [E2E](backend/tests/test_agent_service_integration.py) | Подтверждены JSON summary, исторические ставки и воспроизводимая оценка из PostgreSQL. Реальный billing не проверялся. |
| Евгений объясняет agent loop | [Упражнения и самопроверка](docs/LEARNING_GUIDE_WEEK3.md#20-упражнения-самопроверка-и-вопросы-для-интервью) | Самостоятельный учебный checkpoint отложен. Объяснение владельцем пока не подтверждено. |

**Deferred learning checkpoint.** Владелец продолжает изучать материалы параллельно с дальнейшей разработкой. Осталось самостоятельно объяснить agent loop, границы lifecycle и транзакций, а также сценарий отмены с rollback записи usage: какой статус уже закоммичен, какие события сохраняются и какое исключение получает вызывающий код.

**Ограничения.** Автоматический запуск через API, настоящий доступ к репозиторию, live provider и billing, production load, распределённая отмена и восстановление после сбоя выходят за пределы проверенного offline контура. Фактические часы не сообщены.

**Next step** — Week 4 — Tool System, Permissions & Audit. Статус `not started`.

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

