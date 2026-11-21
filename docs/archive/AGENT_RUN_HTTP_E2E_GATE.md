# Step 11D — Public AgentRun API regression gate

Baseline PR #59: `036ae429502d47bb147cecf211b9e3b9f9de8089`. База `origin/main`: `75f359442f7284eb197163738317f26612793851`.

## Матрица покрытия

| Сценарий | Имеющееся покрытие | Дополнение 11D |
| --- | --- | --- |
| D01 — HTTP create → start → poll | `tests/test_agent_runs_api_integration.py::test_postgres_create_is_committed_and_does_not_start_execution`, `tests/test_agent_runs_start_cancel_integration.py::test_http_start_accepts_before_claim_and_real_offline_graph_reaches_verifying` | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_read_only_workflow[preliminary]` |
| D02 — независимый PASS | `tests/test_verification_e2e_integration.py::test_postgres_read_only_graph_reaches_durable_verdict[pass]` | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_read_only_workflow[pass]` |
| D03 — независимый FAIL | `tests/test_verification_e2e_integration.py::test_postgres_read_only_graph_reaches_durable_verdict[mismatch]` | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_read_only_workflow[fail]` |
| D04 — отмена при pending approval | `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_waiting_approval_keeps_fixture_unchanged` и `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_signals_running_coroutine_after_commit_and_discards_late_result` | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_approved_write_hybrid[cancel]` дополняет наблюдение waiting и terminal status через публичные GET |
| D05 — одобренная запись | `tests/test_verification_e2e_integration.py::test_postgres_approved_read_write_graph_reaches_durable_success` | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_approved_write_hybrid[approve]` |
| D06 — ошибки и отсутствие утечек | HTTP validation, fail-closed и SQL budget покрыты узлами ниже | Проверка payload и журналов на синтетические маркеры в каждом новом сценарии |
| D07 — committed state и cursor | `tests/test_agent_runs_read_integration.py::test_postgres_result_and_events_show_committed_preliminary_from_independent_sessions`, `tests/test_agent_runs_read_integration.py::test_postgres_poll_sees_new_events_only_after_commit_and_closes_sessions` | Сопоставление последовательности всех HTTP events и статуса с независимым PostgreSQL engine |

## HTTP и доверенные операции

| Операция | Граница ответственности |
| --- | --- |
| `POST /tasks` | Task создаётся через публичный API. Workspace предварительно создан серверной fixture. Его ID в запросе выбирает бизнес-ресурс. Runtime binding с sandbox root и scopes заранее передаётся в `create_app()` на стороне сервера. |
| `POST /tasks/{task_id}/runs`, `GET /agent-runs/{run_id}` | `AgentRunService` создаёт начальную попытку с `201` и возвращает metadata replay с `200` для существующей queued attempt #1. Отдельный `GET` возвращает metadata той же попытки. |
| `POST /agent-runs/{run_id}/start` | `InProcessRunCoordinator` принимает локальную задачу и отвечает `202` с `Location`. `AgentService` сохраняет claim, исполняет реальный compiled StateGraph через `AgentRunner` и отдельно сохраняет предварительный результат. ToolCall и AgentEvent записываются реальными сервисами. |
| `POST /agent-runs/{run_id}/cancel` | Доменный переход фиксируется до сигнала локальной coroutine. При pending approval cleanup закрывает Approval и ToolCall; в истории могут появиться audit и usage после terminal status. |
| `GET /agent-runs/{run_id}/events`, `GET /agent-runs/{run_id}/result` | `AgentRunReadService` выдаёт безопасные DTO. Events использует keyset по `sequence`; result одним SQL statement читает run, workspace binding и упорядоченную релевантную историю. На уровне SQL агрегация ограничена 513 событиями, при превышении 512 ответ содержит безопасный `AGENT_RUN_READ_FAILED`. |
| Approval и independent verification | В гибридном тесте `ApprovedRepositoryAgentRunner._decide()` — существующая доверенная in-process граница. `VerificationService.verify()` вызывается доверенным кодом со своим планом, подготовленным из acceptance criteria Task и заранее известных байтов. Терминальный переход и evidence становятся видимыми после commit. |

D01 выполняет весь исследуемый сценарий через HTTP. D02/D03 добавляют внутренний вызов verification к HTTP-started выполнению. D05 добавляет внутренние approval и verification. Тесты используют FastAPI lifespan и HTTPX ASGITransport, PostgreSQL, LangGraph и ChatOpenAI с MockTransport. Модель предлагает tool call. Доверенная конфигурация и существующий ApprovalGate контролируют допуск к записи. Это сохраняет границы ADR-001 и ADR-002. [ADR-001](adr/ADR-001.md), [ADR-002](adr/ADR-002.md), [Step 10D](VERIFICATION_V1_E2E_GATE.md).

## Существующие HTTP и failure gates

Указаны точные pytest node IDs относительно `backend`. Параметризованные узлы показывают конкретные сценарии, все варианты входят в полный прогон.

| Проверка | Pytest node ID |
| --- | --- |
| Missing Task/Run | `tests/test_agent_runs_api.py::test_create_and_get_map_absent_resources` |
| Malformed UUID и body | `tests/test_agent_runs_start_cancel_api.py::test_run_commands_reject_malformed_uuid_and_private_fields[start]` |
| Malformed query | `tests/test_agent_runs_read_api.py::test_events_reject_invalid_cursor_and_limit_without_echo[limit-private-value]` |
| Duplicate initial create | `tests/test_agent_runs_api_integration.py::test_postgres_concurrent_posts_wait_for_task_lock_and_return_one_run` |
| Concurrent Start/Start, duplicate Start, занятая capacity | `tests/test_agent_runs_start_cancel_integration.py::test_http_concurrent_start_conflicts_and_capacity_allows_one_claim` |
| Foreign workspace binding | `tests/test_agent_runs_start_cancel_integration.py::test_http_foreign_workspace_binding_rejects_start_and_cancel_before_composition` |
| Unconfigured runtime | `tests/test_agent_runs_start_cancel_api.py::test_run_commands_fail_closed_without_trusted_execution[start]` |
| Cancel до claim | `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_before_claim_blocks_runner_and_keeps_terminal_status` |
| Cancel во время running и поздний результат coroutine | `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_signals_running_coroutine_after_commit_and_discards_late_result` |
| Cancel при pending approval | `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_waiting_approval_keeps_fixture_unchanged` |
| Cancel во время verifying | `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_verifying_run_wins_before_verification_publication` |
| Terminal cancel conflict | `tests/test_agent_runs_start_cancel_integration.py::test_http_start_rejects_non_queued_and_cancel_rejects_terminal[succeeded]` |
| Client disconnect после начала ответа | `tests/test_agent_runs_start_cancel_integration.py::test_http_request_disconnect_after_response_start_preserves_background_execution` |
| Shutdown при отказе БД | `tests/test_agent_runs_start_cancel_integration.py::test_http_lifespan_shutdown_observes_local_execution[database-unavailable]` |
| Зависшая coroutine записи отмены при shutdown и fallback-сигнал runner | `tests/test_agent_runs_start_cancel_integration.py::test_http_lifespan_shutdown_times_out_unconfirmed_cancel_and_cleans_runner` |
| Partial filesystem apply | `tests/test_approved_repository_integration.py::test_postgres_approved_graph_write_failure_is_audited_without_retry[partial-apply]` |
| Duplicate verification | `tests/test_verification_service_integration.py::test_postgres_concurrent_verifiers_serialize_without_duplicate_evidence[same-attempt]` |
| Commit rollback | `tests/test_verification_service_integration.py::test_postgres_verification_real_event_constraint_rolls_back_evidence_and_status`, `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_constraint_failure_rolls_back_without_signaling_runner` |
| Lost COMMIT acknowledgment | `tests/test_verification_service_integration.py::test_postgres_verification_lost_commit_acknowledgment_replays_durable_attempt` |
| Result budget 512/513, keyset после отказа | `tests/test_agent_runs_read_integration.py::test_postgres_result_history_limit_and_events_polling` |
| Unknown event и порядок cursor | `tests/test_agent_runs_read_integration.py::test_postgres_keyset_traverses_unknown_and_known_events_without_skips[1]`, `tests/test_agent_runs_read_integration.py::test_postgres_keyset_traverses_unknown_and_known_events_without_skips[2]` |
| Corrupted event/result и безопасный 500 | `tests/test_agent_runs_read_integration.py::test_postgres_malformed_known_event_fails_closed_without_echo[verification_recorded]`, `tests/test_agent_runs_read_integration.py::test_postgres_result_fails_closed_on_corrupt_or_contradictory_snapshot[invalid-result]` |
| Binding между независимыми runs | `tests/test_verification_e2e_integration.py::test_postgres_two_graph_runs_cannot_exchange_verification_binding` |
| Sanitized log и HTTP | `tests/test_agent_runs_start_cancel_integration.py::test_http_background_runner_failure_is_observed_and_logs_no_payload`, `tests/test_agent_runs_read_integration.py::test_postgres_offline_graph_public_reads_use_bounded_sql_and_do_not_leak` |

## Что подтверждает PostgreSQL

- Каждый новый сценарий создаёт Task и AgentRun через HTTP, проверяет единственную попытку #1 и один вызов фабрики runner, завершает lifespan и очищает тестовые записи только по UUID созданных fixtures. Рабочий каталог принадлежит конкретному тесту.
- Poll до снятия `asyncio.Event` видит `running` без preliminary. Существующий 11C тест в матрице дополнительно удерживает реальный commit и проверяет отсутствие видимости незакоммиченного preliminary. Последующие страницы продолжают предыдущий cursor и сверяются по `sequence` и типу события с чтением через новый engine. Отдельные HTTP GET могут наблюдать разные committed snapshots при конкурентной записи.
- Для PASS и FAIL новый engine подтверждает evidence, единственный preliminary event, терминальный статус и неизменный `Task.status=queued`. Повтор того же verification attempt возвращает то же evidence, а poll по последнему cursor остаётся пустым. FAIL публикует verdict без execution failure code.
- Одобренная запись даёт ровно один физический replace. При отмене в `waiting_approval` история содержит committed cancel, cleanup approval/tool audit и usage. Запись отсутствует, исходные байты сохранены, повторный completion отклоняется и не добавляет событий.
- HTTP и захваченный лог проверяются на синтетические API key, содержимое fixture, путь, предварительный текст, raw patch, внутренние идентификаторы и ожидаемый digest. Проверка относится к исследуемым DTO, журналам и синтетическим стимулам. Коррупция проверяется отдельными failure gates.

## Среда и исполнение

Python 3.13.0, FastAPI 0.156.8. `app.main` и сервисы импортировались из Week 4 worktree. Опт-ин использует существующие выделенные loopback-базы `forge_task03/04/05/06_week4_audit_4d6f653603` на `127.0.0.1:55237`. Перед SQL-прогонами проверяются фактическое имя каждой базы, PostgreSQL 17.11, `READ COMMITTED` и Alembic `1d972cc34fbe`. Скрипт получает логин и пароль из локального профиля. Для ordinary прогона скрипт снимает PostgreSQL opt-ins. Тесты отключают tracing и используют offline model transport.

RED покрытия до создания тестового файла. `pytest --collect-only` на 11A–11C и 10D с `-k 'test_http_11d_read_only or test_http_11d_approved_write'` дал код 5 и `no tests collected (76 deselected)`. Это отсутствие сквозных тестов. Baseline HTTP gate 11A–11C дал `94 passed`.

| Прогон после добавления покрытия | Результат |
| --- | --- |
| Owner `--focused`, пять новых node IDs из матрицы | 5 passed |
| Owner `--http`, все HTTP-тесты 11A–11D | 99 passed |
| 09A/09B, `test_repository_agent_integration.py` и `test_approved_repository_integration.py` | 26 passed |
| 10A–10C, `test_verification_contract.py`, `test_verification_observer.py`, `test_verification_evidence.py` | 106 passed |
| 10C/10D, `test_verification_service_integration.py` и `test_verification_e2e_integration.py` | 63 passed |
| Owner `--full`, ordinary backend | 1489 passed, 409 PostgreSQL opt-in skipped |
| Owner `--postgres`, пять групп с guards для каждой базы | 409 passed (403 + 2 + 2 + 1 + 1) |

Итоговые прогоны дали 0 failures и 0 pytest warnings. `python -m pip check` вернул `No broken requirements found`. Проверка whitespace включает оба новых файла. Для owner verification доступен отдельный локальный zsh-скрипт с режимами из таблицы, локальным профилем PostgreSQL и проверкой путей импортов. Числа выше получены Codex в текущем worktree. Проверка владельцем опубликованного HEAD остаётся отдельным шагом.

## Границы и готовность Step 11

- `202 Accepted` означает локальное принятие координатором. PostgreSQL остаётся источником бизнес-состояния. После graceful shutdown сервис ограниченно ждёт фиксацию отмены и завершение локальной задачи. Если результат фиксации неизвестен, он отправляет локальный сигнал отмены и журналирует неопределённость. Coroutine, которая подавляет cancellation или блокирует event loop, может не завершиться вовремя. Тест таймаута shutdown моделирует зависшую запись отмены, а тест сбоя БД проверяет fallback для кооперативного runner.
- Продолжение исполнения после рестарта и distributed/multi-worker ownership отложены. Факт сохранения metadata, audit или evidence в PostgreSQL не обеспечивает автоматическое возобновление.
- Публичная аутентификация и авторизация остаются за рамками Step 11. Foreign workspace gate проверяет конкретный доверенный execution binding. Публичных операций принятия approval и запуска verification в этом контракте нет.
- Проверка файловой системы ограничена доверенными изолированными fixtures. Наблюдение байтов не устанавливает авторство изменений, а конкурирующая внешняя мутация и filesystem TOCTOU остаются ограничениями Step 10. Частично применённая операция отражается в аудите, её эффекты могут сохраниться.
- Данные достаточны для рассмотрения Step 11 владельцем в PR #59 по согласованному локальному HTTP-контракту. M1 остаётся открытым, готовность к production не проверялась.
