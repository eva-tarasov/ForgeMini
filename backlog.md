# Forge backlog

Записи фиксируют наблюдаемый долг и вопросы, а не утверждённые сроки. Приоритеты предложены для review; target milestone без подтверждения указан как «не определён».

## BL-01 — Валидация при прямом вызове TaskService
- Type: debt; Priority: medium; Status: open
- Context / Evidence: `TaskCreate` проверяет строки на HTTP-границе, но `TaskService.create_task()` принимает обычные значения и напрямую их не проверяет.
- Target milestone: не определён.
- Acceptance criteria: владельцем определена граница валидации для не-HTTP вызовов; выбранное поведение проверено тестами.

## BL-02 — Передача request_id в нижние слои
- Type: improvement; Priority: medium; Status: open
- Context / Evidence: `log_http_request()` создаёт ID для ответа и записи, но не передаёт его сервисам.
- Target milestone: не определён.
- Acceptance criteria: определён способ безопасной корреляции событий нижних слоёв; распространение и изоляция запросов проверены.

## BL-03 — Конфиденциальность access logs Uvicorn
- Type: decision; Priority: medium; Status: open
- Context / Evidence: собственный `forge.http` пишет шаблон маршрута, но конфигурация стандартного Uvicorn access log отдельно не проверена.
- Target milestone: не определён.
- Acceptance criteria: проверено фактическое содержимое access logs и задокументирована безопасная конфигурация без чувствительных URL и query.

## BL-04 — Форматирование других событий
- Type: improvement; Priority: low; Status: deferred
- Context / Evidence: `JsonFormatter.format()` ожидает `record.http_fields` и рассчитан на HTTP-события.
- Target milestone: не определён.
- Acceptance criteria: при появлении других событий определены их схема и logger; обычное HTTP-логирование сохраняется.

## BL-05 — Автоматические проверки GitHub
- Type: improvement; Priority: medium; Status: open
- Context / Evidence: в текущем checkout нет `.github/workflows`; состояние настроек удалённого репозитория отдельно не проверено.
- Target milestone: не определён.
- Acceptance criteria: согласован минимальный набор проверок; результаты запуска доступны для изменений в репозитории.

## BL-06 — Предупреждение Starlette TestClient
- Type: debt; Priority: low; Status: open
- Context / Evidence: локальный pytest воспроизводит `StarletteDeprecationWarning` о `httpx` и `starlette.testclient`.
- Target milestone: не определён.
- Acceptance criteria: совместимый путь обновления зависимостей выбран и проверен; тесты проходят без этого предупреждения.

## BL-07 — Идемпотентность при редактировании Task
- Type: architecture/reliability debt. Status: deferred.
- Context / Evidence: при replay сервис сравнивает нормализованный `goal` и упорядоченные `acceptance_criteria` с текущими полями Task. Неизменяемый снимок исходного запроса или fingerprint пока не хранится. [TaskService](backend/app/services/tasks.py)
- Risk: если эти поля станут редактируемыми, повтор исходного запроса после изменения может получить `409 IDEMPOTENCY_KEY_CONFLICT`.
- Target milestone: до появления редактируемых `goal` или `acceptance_criteria`.
- Acceptance criteria: определить, как сохранять исходную идентичность идемпотентного запроса. Проверить replay и конфликт с другим payload в PostgreSQL integration tests.

## BL-08 — Сериализация нумерации AgentEvent
- Type: concurrency/reliability debt. Status: closed. [PR #22](https://github.com/eva-tarasov/myFirstForge/pull/22) merged в `main` (`2aaeae5`).
- Contract: `AgentEventRepository.append()` блокирует родительский AgentRun без autoflush, явно выполняет flush и отдельным запросом вычисляет `COALESCE(MAX(sequence), 0) + 1` при PostgreSQL `READ COMMITTED`. Status writer использует этот же контракт через `add_status_changed()`. Commit и rollback принадлежат caller. [AgentEventRepository](backend/app/repositories/agent_events.py)
- Evidence: 15 новых PostgreSQL cases прошли. `pg_blocking_pids()` подтвердил ожидание блокировки для двух generic writers и для generic/status в обоих порядках. После commit история имеет sequence 1, 2. После rollback ожидающий writer повторно использует номер 1. Разные AgentRun нумеруются независимо. [Integration tests](backend/tests/test_agent_event_append_integration.py)
- Regression correction: `test_event_repository_sequences_and_does_not_commit` проверяет блокировку внутри `no_autoflush`, порядок `lock → flush → MAX → add` и владение commit/rollback. Обычный pytest после исправления дал `429 passed, 67 skipped` без failures и warnings. Повторный PostgreSQL suite дал `61 passed`.
- Приняты три ручных сценария по отчёту владельца: concurrent append, append с выключенным autoflush и rollback.
- Remaining boundary: все будущие writers используют общий append contract в коротких транзакциях для одного AgentRun. `UNIQUE(agent_run_id, sequence)` остаётся финальной защитой.

## BL-09 — Уровень изоляции транзакций при replay
- Type: architecture/reliability consideration. Status: deferred.
- Context / Evidence: после `ON CONFLICT DO NOTHING` репозиторий выполняет отдельный SELECT. При `READ COMMITTED` SELECT получает новый snapshot и может прочитать строку, зафиксированную конкурирующей транзакцией. [TaskRepository](backend/app/repositories/tasks.py)
- Risk: смена уровня изоляции без пересмотра алгоритма может изменить результат replay, в том числе привести к ошибке сериализации.
- Target milestone: до изменения уровня изоляции транзакций.
- Acceptance criteria: зафиксировать требование `READ COMMITTED` для текущего алгоритма. Перед сменой уровня проверить на PostgreSQL конкурентное создание с одинаковым ключом и разными payload, а также сценарий rollback и повторного использования ключа.
