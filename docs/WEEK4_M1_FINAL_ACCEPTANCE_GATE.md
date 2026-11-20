# Week 4 · M1 Final Acceptance Gate

**Step 13C:** in progress, пакет готов к review. **M1 Final Acceptance:** pending, решение принимает владелец.

**Baseline:** `b86ff2cdcb439e124a2062e2688efca8dde1a652`, squash [PR #63](https://github.com/eva-tarasov/myFirstForge/pull/63). Step 12 merged в `e1c23fb7edc0fe362f640afb3a73f578b88c8eba`. [PR #61](https://github.com/eva-tarasov/myFirstForge/pull/61)

Все семь критериев имеют техническое evidence для доверенного локального fixture. Матрица сохраняет исходные критерии M1. Учебная приёмка и финальное решение владельца учитываются отдельно. [Прогресс Week 4](../forge_progress.md#прогресс-week-4)

## Критерии

Команды с node ID запускаются из `backend`. Для PostgreSQL-сценариев сначала применяется guard ниже.

| Критерий | Реализованный контракт и код | Точный тест и evidence | Граница | Статус |
| --- | --- | --- | --- | --- |
| 1. Task → AgentRun → ToolCall → Result | Task сохраняется отдельно. POST создаёт начальную попытку, Start отдаёт coroutine локальному coordinator, runner исполняет инструмент, итог читается из committed projection. [AgentRun API](../backend/app/api/agent_runs.py) · [AgentService](../backend/app/services/agent_service.py) | `tests/test_agent_runs_http_e2e_integration.py::test_http_11d_read_only_workflow`. Проверены 202, реальный offline граф, запись ToolCall, polling и исходы verification. | Один локальный worker с доверенным binding. Публичная аутентификация, restart recovery и scheduling остаются открытыми. | PASS · локальный |
| 2. Repository read | Read tools получают bound root, capability и логические scopes. Audited executor возвращает typed output после terminal audit. [Фабрика](../backend/app/tools/repository/factory.py) · [Reader](../backend/app/tools/repository/filesystem.py) | `tests/test_repository_agent_integration.py::test_postgres_repository_agent_reads_fixture_and_persists_preliminary_result`. Offline SDK callback видит реальные байты, корреляцию и committed result. Пятый запуск владельца подтверждает `read_file / succeeded` у live provider. | Бюджеты чтения и локальные доверенные POSIX fixtures. BL-10 остаётся открытым. | PASS · offline и bounded live |
| 3. Controlled write | `apply_patch` обслуживает ограниченный unified diff для существующих текстовых fixture файлов. Writer повторно сверяет исходник и заменяет по файлу. [Parser](../backend/app/tools/repository/patch.py) · [Writer](../backend/app/tools/repository/writer.py) | `tests/test_public_http_e2e_integration.py::test_http_12e_approve_to_verified_pass` и `tests/test_repository_patch.py::test_fixture_patch_reports_partial_application_without_rollback`. Точная замена выполнена один раз, частичный эффект воспроизводим. | Одна замена атомарна в пределах поддерживаемой файловой операции. Multi-file rollback и durable compensation отсутствуют. Live gate read-only. | PASS · offline |
| 4. Approval gate | Раздельные capability разрешают чтение или точное review/decision. Approve связан с fingerprint, операцией и сохранённым запросом. Claim отдельно проверяет состояние и однократно допускает write. [Decision service](../backend/app/services/approval_decisions.py) · [ApprovalGate](../backend/app/services/approval_gate.py) | `tests/test_approval_decision_integration.py::test_postgres_http_concurrent_approve_records_once_then_executes_and_verifies` и `tests/test_public_http_e2e_integration.py::test_http_12e_reject_has_no_write_or_verification`. Проверены competing decisions, reject и пределы side effects. | Capability подтверждает владение токеном. Pending waiter живёт в одном процессе, персональная аутентификация и durable resume остаются открытыми. | PASS · offline |
| 5. Полный audit trail | Отдельные ToolCall и AgentEvent фиксируют допуск, наблюдаемый исход и связанные переходы. Append сериализуется родительским row lock. Публичные ответы содержат безопасные проекции. [ToolCallService](../backend/app/services/tool_calls.py) · [AgentEventRepository](../backend/app/repositories/agent_events.py) | `tests/test_tool_call_integration.py::test_postgres_audited_read_file_persists_metadata_only_history` и `tests/test_agent_event_append_integration.py::test_agent_event_append_serializes_concurrent_writers`. Согласованные записи, уникальный sequence, безопасные метаданные. | Полнота относится к проверенным фазам и поддерживаемому scope. Crash между SQL и filesystem может оставить эффект или состояние неустановленным. | PASS · поддерживаемые lifecycle-сценарии |
| 6. Path traversal protection | Сначала канонизируются компоненты логического пути и проверяется scope. Затем применяется descriptor-relative POSIX access с проверкой типа, identity и запретом symlinks. [Логические пути](../backend/app/permissions/paths.py) · [Filesystem reader](../backend/app/tools/repository/filesystem.py) | `tests/test_safe_repository.py::test_reader_denies_logical_paths_before_opening_root` и `tests/test_safe_repository.py::test_reader_refuses_intermediate_symlink_substitution`. Отказ до доступа вне scope и при подмене каталога. | Trusted local fixture и предки корня. Остаточные TOCTOU и отсутствие OS sandbox учитываются отдельно. | PASS · тестируемая fixture-граница |
| 7. Success требует verification evidence | Ready result переводит run в `verifying`. Trusted verifier повторно читает данные, проверяет audit и атомарно сохраняет evidence и terminal verdict при PASS/FAIL. [VerificationService](../backend/app/services/verification_service.py) · [Guard прямого перехода](../backend/app/services/agent_runs.py) | `tests/test_verification_e2e_integration.py::test_postgres_read_only_graph_reaches_durable_verdict[pass]`, та же проверка `[mismatch]` и `tests/test_verification_service_integration.py::test_postgres_direct_success_guard_remains_and_task_is_unchanged`. Владелец подтвердил live PASS. | Evidence ограничено наблюдавшимися байтами и audit. Авторство изменений и физическая изоляция требуют отдельного доверенного механизма. | PASS · offline и bounded live |

## Offline post-merge evidence

Проверка 20 ноября 2026 на merge SHA выше. Python 3.13.0 в `/Users/dev/Desktop/forge/backend/.venv/bin/python`, тесты и импорты из Week 4 worktree. Guard сверил на `127.0.0.1:50088` фактические имена всех четырёх баз, PostgreSQL 17.11, `READ COMMITTED` и Alembic `1d972cc34fbe`. Для модели использовался MockTransport.

| Прогон | Фактический результат |
| --- | --- |
| Обычный backend pytest с выключенными opt-in | **1622 passed, 481 skipped** |
| Backend pytest с opt-in Task 06 | **2099 passed, 4 skipped** |
| Отдельные группы Task 03 / 04 / 05 | **2 / 1 / 1 passed** |
| Итог opt-in по четырём БД | **481 дополнительный сценарий** из обычных skips. 477 в Task 06, ещё 4 в отдельных группах |
| HTTP E2E 11D и 12E отдельным прогоном | **9 passed**, входят в opt-in coverage |
| Smoke dry-run | `OFFLINE_PASS`, 3 mock requests, `read_file / succeeded`, стадия `finalization`, verification `pass`, 90 синтетических токенов, cleanup `completed` |
| `pip check` | `No broken requirements found.` |

Все pytest-прогоны завершились без failures и pytest warnings. В обычном и opt-in прогоне есть пересечение по 1622 обычным случаям. Четыре skips Task 06 относятся к отдельным guard для Task 03–05 и закрыты соответствующими прогонами. Всего **2103 уникальных backend cases**. [Исторический E2E verification gate](VERIFICATION_V1_E2E_GATE.md) · [Исторический Step 12E gate](STEP_12_HTTP_E2E_GATE.md)

## Live evidence от владельца

Пятый bounded live запуск завершился на feature HEAD `cffc15f0c820e2d470b5be4f063ada6066108ac5` в `2026-11-20T13:28:00Z`. Итог `LIVE_PASS`, exit 0. Выполнены три model requests, последняя стадия `finalization`, один `read_file / succeeded`, independent verification `pass`, cleanup `completed`. `usage_complete=true`, `known_total_tokens=1068`, `known_cost_usd=null`, `cost_complete=false`. Фактическая стоимость UNKNOWN.

Предыдущие попытки хранятся в live gate. Причина третьего отказа остаётся **UNKNOWN**. Прямой repository evidence в финализации — открытый [BL-10](../backlog.md#bl-10--передача-repository-evidence-в-finalization). Владельческий live evidence относится к одному read-only fixture. Post-merge прогон воспроизвёл контракт с MockTransport. [Все пять попыток](WEEK4_M1_LIVE_SMOKE_GATE.md)

## Воспроизведение бесплатной регрессии

Запускайте из корня Forge. `FORGE_M1_PYTHON` можно задать для уже установленного окружения, если путь отличается. Команды рассчитаны на macOS zsh. PostgreSQL-блок использует существующий доверенный профиль без вывода пароля. Если профиль недоступен, результаты PostgreSQL, HTTP и dry-run для этого прогона остаются pending до проверки выделенных баз.

### Ordinary backend

```zsh
(
  set -e
  set +x
  cd "$(git rev-parse --show-toplevel)/backend"
  unset PYTHONPATH PYTEST_ADDOPTS OPENAI_API_KEY OPENAI_LOG FORGE_WEEK4_LIVE_SMOKE
  unset FORGE_TEST_POSTGRES FORGE_TEST_SCHEMA_DB FORGE_TASK04_TEST_DB FORGE_TASK05_TEST_DB FORGE_TASK06_TEST_DB
  unset FORGE_TOOL_CALL_TEST_DB FORGE_APPROVAL_TEST_DB FORGE_REPOSITORY_AGENT_TEST_DB
  export PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
  forge_python=${FORGE_M1_PYTHON:-/Users/dev/Desktop/forge/backend/.venv/bin/python}
  "$forge_python" -m pytest -q -p no:cacheprovider
  "$forge_python" -m pip check
  git diff --check
  git status --short
)
```

### PostgreSQL opt-in, HTTP E2E и smoke dry-run

```zsh
(
  set -e
  set +x
  cd "$(git rev-parse --show-toplevel)/backend"
  unset PYTHONPATH PYTEST_ADDOPTS OPENAI_API_KEY OPENAI_LOG FORGE_WEEK4_LIVE_SMOKE
  unset FORGE_TEST_POSTGRES FORGE_TEST_SCHEMA_DB FORGE_TASK04_TEST_DB FORGE_TASK05_TEST_DB FORGE_TASK06_TEST_DB
  unset FORGE_TOOL_CALL_TEST_DB FORGE_APPROVAL_TEST_DB FORGE_REPOSITORY_AGENT_TEST_DB
  export PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
  forge_python=${FORGE_M1_PYTHON:-/Users/dev/Desktop/forge/backend/.venv/bin/python}
  source "${FORGE_M1_PG_PROFILE:-/private/tmp/forge-12c2.hhPAQI/loopback.zsh}"
  [[ "$POSTGRES_HOST" == 127.0.0.1 && "$POSTGRES_PORT" == 50088 && "$POSTGRES_DB" == forge_task06_week4_audit_4d6f653603 ]]
  "$forge_python" - <<'PY'
import asyncio
import os
import asyncpg

async def verify():
    for phase in ("03", "04", "05", "06"):
        expected = f"forge_task{phase}_week4_audit_4d6f653603"
        connection = await asyncpg.connect(
            host="127.0.0.1", port=50088, database=expected,
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], timeout=5,
        )
        try:
            async with connection.transaction(readonly=True):
                identity = await connection.fetchrow(
                    "SELECT current_database(), current_setting('server_version_num'), "
                    "current_setting('transaction_isolation'), (SELECT version_num FROM alembic_version)"
                )
                if tuple(identity) != (expected, "170011", "read committed", "1d972cc34fbe"):
                    raise ValueError("postgres_identity_mismatch")
                print(expected, "17.11", "read committed", "1d972cc34fbe")
        finally:
            await connection.close()

try:
    asyncio.run(verify())
except Exception:
    raise SystemExit("Проверка выделенных PostgreSQL-баз не прошла.") from None
PY
  export FORGE_TASK06_TEST_DB="$POSTGRES_DB" FORGE_TOOL_CALL_TEST_DB="$POSTGRES_DB" FORGE_APPROVAL_TEST_DB="$POSTGRES_DB"
  export FORGE_REPOSITORY_AGENT_TEST_DB="$POSTGRES_DB" FORGE_TEST_POSTGRES=1
  "$forge_python" -m pytest -q -p no:cacheprovider
  POSTGRES_DB=forge_task03_week4_audit_4d6f653603 FORGE_TEST_SCHEMA_DB=forge_task03_week4_audit_4d6f653603 \
    "$forge_python" -m pytest -q -p no:cacheprovider tests/test_schema_integration.py
  POSTGRES_DB=forge_task04_week4_audit_4d6f653603 FORGE_TASK04_TEST_DB=forge_task04_week4_audit_4d6f653603 \
    "$forge_python" -m pytest -q -p no:cacheprovider tests/test_repository_integration.py
  POSTGRES_DB=forge_task05_week4_audit_4d6f653603 FORGE_TASK05_TEST_DB=forge_task05_week4_audit_4d6f653603 \
    "$forge_python" -m pytest -q -p no:cacheprovider tests/test_execution_schema_integration.py
  "$forge_python" -m pytest -q -p no:cacheprovider \
    tests/test_agent_runs_http_e2e_integration.py tests/test_public_http_e2e_integration.py
  "$forge_python" -m scripts.week4_live_smoke --dry-run
)
```

## Открытые проверки и решение владельца

- [ ] Принять технические результаты и границы локального fixture в review Step 13C.
- [ ] Подтвердить самостоятельный учебный checkpoint Week 4 по [учебнику](LEARNING_GUIDE_WEEK4.md) либо записать его как отложенный отдельным решением.
- [ ] Зафиксировать решение владельца по M1 и допуску к Week 5. MCP остаётся `not started`.

В этом gate использован macOS. Linux, production load, публичная аутентификация, изоляция недоверенного workspace и durable recovery здесь не проверялись. Новые live-запросы и billing reconciliation не выполнялись. CLI-01 и CLI-02 merged, CLI-03 открыт. Клиентский трек ведётся отдельно от исходного M1.
