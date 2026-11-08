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
