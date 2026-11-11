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
- Type: concurrency/reliability debt. Status: deferred.
- Context / Evidence: репозиторий вычисляет `COALESCE(MAX(sequence), 0) + 1` в пределах AgentRun. Текущий сервис переходов удерживает `SELECT FOR UPDATE` на родительской строке до конца транзакции. [AgentEventRepository](backend/app/repositories/agent_events.py) · [AgentRunRepository](backend/app/repositories/agent_runs.py) · [AgentRunService](backend/app/services/agent_runs.py)
- Risk: новый независимый писатель без этой блокировки может выбрать тот же номер и получить ошибку уникальности.
- Target milestone: до добавления новых путей записи AgentEvent.
- Acceptance criteria: задокументировать и обеспечить единый контракт append. Все писатели используют блокировку родителя или эквивалентную защиту от гонки. Конкурентный append покрыт PostgreSQL integration tests.

## BL-09 — Уровень изоляции транзакций при replay
- Type: architecture/reliability consideration. Status: deferred.
- Context / Evidence: после `ON CONFLICT DO NOTHING` репозиторий выполняет отдельный SELECT. При `READ COMMITTED` SELECT получает новый snapshot и может прочитать строку, зафиксированную конкурирующей транзакцией. [TaskRepository](backend/app/repositories/tasks.py)
- Risk: смена уровня изоляции без пересмотра алгоритма может изменить результат replay, в том числе привести к ошибке сериализации.
- Target milestone: до изменения уровня изоляции транзакций.
- Acceptance criteria: зафиксировать требование `READ COMMITTED` для текущего алгоритма. Перед сменой уровня проверить на PostgreSQL конкурентное создание с одинаковым ключом и разными payload, а также сценарий rollback и повторного использования ключа.
