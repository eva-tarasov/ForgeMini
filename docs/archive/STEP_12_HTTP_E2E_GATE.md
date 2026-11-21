# Step 12E — HTTP E2E gate

Baseline `620f73ce325bd4bcb3143ed13c89a29040d68f0f`, PR #61. Матрица составлена до добавления сценариев.

| Поведение | Существующее покрытие | Недостающая сквозная проверка | Сценарий 12E |
| --- | --- | --- | --- |
| Запуск, approve, однократная запись, независимый PASS | `tests/test_approval_decision_integration.py::test_postgres_http_concurrent_approve_records_once_then_executes_and_verifies` | Discovery и details ведут к тому же approval. После HTTP approve клиент видит переходы в status, result, events и evidence, сверенные с durable состоянием. | `tests/test_public_http_e2e_integration.py::test_http_12e_approve_to_verified_pass` |
| Reject и отказ от повторной записи | `tests/test_approval_decision_integration.py::test_postgres_http_reject_is_recorded_once_without_writing` | Обнаружение, точный review и reject относятся к одному run. HTTP details, result, events и пустой evidence отражают committed rejection и отказ от replay/overwrite. | `tests/test_public_http_e2e_integration.py::test_http_12e_reject_has_no_write_or_verification` |
| INCONCLUSIVE, FAIL и отмена | `tests/test_verification_evidence_api_integration.py::test_postgres_offline_graph_evidence_paginates_real_attempts[fail]`, `tests/test_verification_evidence_api_integration.py::test_postgres_offline_graph_evidence_paginates_real_attempts[cancelled]`, `tests/test_agent_runs_start_cancel_integration.py::test_http_cancel_verifying_run_wins_before_verification_publication` | Один HTTP-started run показывает тот же INCONCLUSIVE в result/events/evidence, затем сохраняет его после FAIL или HTTP cancel. Повтор запуска и новая проверка закрытого run отклоняются. | `tests/test_public_http_e2e_integration.py::test_http_12e_inconclusive_remains_visible_across_terminal_paths[fail]` и `tests/test_public_http_e2e_integration.py::test_http_12e_inconclusive_remains_visible_across_terminal_paths[cancel]` |
| Capability и область workspace | `tests/test_approvals_api_integration.py::test_postgres_capability_precedes_run_lookup_and_foreign_run_is_indistinguishable`, `tests/test_approval_review_integration.py::test_postgres_review_is_scoped_and_cold_pending_is_not_reviewable`, `tests/test_verification_evidence_api_integration.py::test_postgres_evidence_scope_and_empty_run` | Read/review credentials, чужой workspace и неизвестные IDs проверяются на том же живом approval и сохранённом evidence. | Встроено в approve/PASS |
| Гонки и commit | `tests/test_approval_decision_integration.py::test_postgres_http_decision_fails_closed_when_lifecycle_ends[cancel]`, `tests/test_approval_decision_integration.py::test_postgres_lost_response_reconciles_committed_approval_before_cancellation`, `tests/test_verification_evidence_api_integration.py::test_postgres_evidence_observes_terminal_attempt_only_after_commit` | Во время одного approve/PASS сценария UI-проекции вместе переходят с committed preliminary на committed PASS. Существующие тесты остаются gates для гонок decision/cancel и потери ответа. | Встроено в approve/PASS |
| Повреждённое evidence и replay | `tests/test_verification_evidence_api_integration.py::test_postgres_evidence_rejects_corrupt_history_before_selecting_page[duplicate-attempt]`, `tests/test_approval_decision_integration.py::test_postgres_gate_rollback_leaves_no_decision_or_partial_audit` | Повреждение, rollback и гонка двух approve уже проверяются реальным PostgreSQL. Публичный отказ повторного decision/start проверяется в исходном E2E run. | Встроено в approve/PASS и reject |

Четыре pytest-узла закрывают пробелы между API. Отдельные сценарии для уже проверенных повреждений, rollback, потери ответа и гонки decision/cancel повторно не добавляются.

## Как проходит gate

- Approve/PASS использует offline approved LangGraph, реальный lifespan FastAPI, HTTPX ASGITransport и тот же approval на discovery, details, exact review и decision. После `200 recorded=true` тест удерживает `ApprovalGate.claim()` перед допуском записи и видит исходные байты, ToolCall со статусом `proposed` и сохранённый approval `approved`. После снятия барьера фиксируется ровно один replace, а status/result/evidence показывают committed preliminary.
- Независимый `VerificationService` вызывается из доверенного тестового кода. Сценарий PASS удерживает его транзакцию после flush terminal status event и проверяет, что status, result, events и evidence ещё показывают предыдущий committed snapshot. После commit evidence и terminal transition видны в согласованном порядке. HTTP-проекции сверяются с чтением через новый PostgreSQL engine. Повторный decision, start и cancel на успешном run отклоняются, а повторные GET не вызывают verifier или файловый observer.
- Reject оставляет исходные байты и подтверждает одну запись `rejected`. Конкретная причина `approval_rejected` сохраняется в ToolCall audit. Код уровня AgentRun в публичном `/result` — `graph_execution_failed`. На первом прогоне ошибочное ожидание более узкого кода дало 3 passed и 1 failed. Assertion приведён в соответствие с существующим контрактом.
- В двух вариантах INCONCLUSIVE отдельный read-only LangGraph запускается через HTTP. Совпадение байтов при `isolated_fixture=false` остаётся `inconclusive` и не завершает run. Затем либо независимая verification подтверждает mismatch и завершает run в `failed`, либо HTTP cancel завершает run в `cancelled`. Ранее сохранённая попытка по-прежнему видна, а новая попытка verification и повторный start не переоткрывают terminal run.
- Проверки scope используют собственный и чужой workspace на реальном approval и сохранённом evidence. Read/review credentials не взаимозаменяемы. Exact review и evidence сохраняют `Cache-Control: no-store`. Точный patch и его fingerprint проверяются только в авторизованном exact review. Из остальных ответов и журналов исключены тестовые секреты, patch, hashes и абсолютный корень. Relative paths входят в разрешённые DTO.

## Среда и ограничения

Тесты используют выделенный loopback PostgreSQL 17.11, `READ COMMITTED`, Alembic `1d972cc34fbe`, UUID-scoped SQL fixtures и disposable repository. Локальные barrier waits ограничены таймаутами. HTTP-клиенты, lifespan и SQL-сессии закрываются, координатор освобождает исполнение. Все ответы модели приходят из offline MockTransport.

ASGITransport проверяет приложение в одном процессе. Реальный сетевой прокси, human identity, durable recovery и production sandbox остаются за пределами gate. Read capability и review capability подтверждают владение токеном для workspace. Stored evidence подтверждает наблюдавшиеся байты при заданных условиях. Авторство изменения остаётся за пределами этого контракта. Уже выполненный side effect не откатывается автоматически.

## Проверка

| Прогон | Результат |
| --- | --- |
| Исходный focused Step 12A–12D | 140 passed |
| Новый Step 12E на PostgreSQL | 4 passed |
| HTTP Step 11/12 | 243 passed |
| Verification contract, observer и codec | 106 passed |
| Verification service и E2E PostgreSQL | 63 passed |
| Ordinary backend | 1593 passed, 449 skipped |
| Полный PostgreSQL opt-in | 449 passed |

До написания тестов collection с селектором `test_http_12e` вернул код 5 и `32 deselected`. Это пробел в покрытии. В итоговых прогонах по 0 failures и 0 pytest warnings. В отдельном 12E и полном opt-in прогонах PostgreSQL skips отсутствовали. `pip check` не нашёл конфликтов зависимостей.
