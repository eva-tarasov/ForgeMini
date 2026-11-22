# ForgeMini — прогресс

Формат: коротко. Подробности и «почему» — в описаниях PR. Старая история M1 — `docs/archive/forge_progress.md`.

## Цель прототипа (v0.1-prototype, ~3 дня)
`forge solve --repo <path> --goal "<что исправить>" --test "<команда тестов>"` (не `forge run`: группа `run` уже занята create/start/watch) →
агент чинит код в Docker-sandbox (без сети) → Verifier в чистом контейнере прогоняет тесты →
`succeeded` + проверенный diff, либо `failed` + понятное объяснение.

## Статус

| День | Шаг | Статус |
|---|---|---|
| 0 | История myFirstForge → ForgeMini, тег `forge-m1-baseline` | история в `claude/main` (push в `main`/теги из сессии запрещены прокси); **тег ещё не поставлен — владельцу** |
| 0 | CLAUDE.md, PROGRESS.md, архив доков | ✅ PR #1 |
| 0 | CI (lint / unit / integration на PG 17) | ✅ PR #1 |
| 1 | DockerSandbox, tools, explicit agent loop, SandboxRunner, `examples/demo-calc` | ✅ PR «Day 1» (на ревью) |
| 2 | Workspace (repo/test_command/image), Verifier, `bootstrap.py`, шаги агента в events, `forge solve/report/patch` | ✅ PR «Day 2» (на ревью) |
| 3 | Живой прогон на Mac владельца, тюнинг, тег `v0.1-prototype` | — |

## Базовая линия (для статьи: «до»)
- Unit: 1622 passed / 481 skipped (без БД) на момент форка; после Day 1: 1669 passed (+47 тестов sandbox). Integration (PG 16 локально): 424 passed / 29 skipped.
- CLI: 754 passed + 8 deselected (жёстко прописан путь `/Users/.../backend/.venv` в demo-launcher). После Day 2: 770 passed.
- После Day 2: unit 1711 passed / 495 skipped; integration (скрипт, PG 16 + Docker) 467 passed / **0 skipped** (раньше 29 тихо пропускались — `test_tool_call_integration.py` требовал свою переменную; теперь включена в скрипт).
- `test_week4_live_smoke.py` исключён из CI: привязан к одной dev-БД и PostgreSQL 17.0.11.
- Ruff: правила E9+F (реальные ошибки); стиль выключен. Исправлен один неиспользуемый импорт.

## Решения
- **Sandbox через Docker CLI, не SDK**: на одну зависимость меньше, одинаково на Linux/macOS. Код копируется в контейнер (`tar`), а не монтируется: агент физически не может писать в ваш checkout.
- **Changeset вместо `git diff` в контейнере**: в `python:3.12-slim` нет ни `git`, ни `patch`. Единица истины — content-addressed changeset (sha256); diff для человека строится из него. Verifier применит *тот же* changeset к чистому исходнику.
- **Ошибка инструмента возвращается модели** (`ToolMessage(status="error")`), а не роняет run. В M1 было наоборот — отсюда 4 «UNKNOWN» в live-smoke.
- **Свой цикл ~100 строк вместо LangGraph**: нужные нам гарантии (бюджет шагов, видимость ошибок) явно читаются в одном файле.
- `succeeded` не выставляется нигде в этом слое: runner отдаёт `ready_for_verification` / `blocked`.
- Тесты интеграции: вместо правки ~24 файлов с 5 разными guard-паттернами имён БД — скрипт `scripts/ci/integration_tests.sh`
  создаёт БД под каждый паттерн. Дёшево и без риска сломать тесты; единый флаг — позже, при чистке.

- **Verifier (Day 2) — правила вердикта.** PASS только если: (1) тесты красные на нетронутом исходнике (baseline), (2) зелёные с changeset в *свежем* контейнере, (3) не изменены/удалены тестовые файлы, (4) changeset не пуст. Baseline уже зелёный → INCONCLUSIVE (нечего чинить); падение инфраструктуры → INCONCLUSIVE, не FAIL. `succeeded` ставит только Verifier.
- **Run-конфигурация живёт в Workspace** (`source_path`, `test_command`, `image`), а не в Task: один репозиторий — много задач. `bootstrap.py` — composition root: в M1 `app = create_app()` не имел привязки исполнения (503).
- **Шаги агента — публичные события `agent_step`** (сжатые head+tail), чтобы `forge solve` мог показывать прогресс; старый строгий read-API расширен, а не обойдён.
- **Контракт CLI не ломаем**: новые поля workspace (`source_path/test_command/image`) не сериализуются, когда `null` — JSON настроенного/ненастроенного workspace совместим с прежним (тесты `cli03 bytes`).
- INCONCLUSIVE пока отображается как run `failed` (отдельного статуса нет) — причина видна в `forge report`.

## Находки по ходу (для статьи)
- Мой первый тест на «нет сети» был бы зелёным и без изоляции: в облачной среде интернета нет вообще. Заменён на проверку «в контейнере только интерфейс `lo`»; проверено, что без `--network none` появляется `eth0`.
- Мутационная проверка ключевого теста (модель видит ошибки инструментов): сломал код — тест упал, вернул — зелёный.
- **Verifier проверен мутацией.** Первый «сценарий читера» (правка теста, который и так не мог пройти) был слабым. Заменён настоящей атакой — ослабить ожидание в `tests/test_average.py`; с выключенной защитой protected-файлов она даёт ложный PASS, с включённой — FAIL. Тест реально охраняет правило.
- **«Зелёный» прогон CLI проверял старый код.** Editable-установка в venv указывала на каталог `myfirstforge/`, а не на `forgemini/`: 754 passed относились к старой копии. Поймано, только когда импорт нового модуля упал. Урок: после смены каталога — `python -c "import pkg; print(pkg.__file__)"`; backend-тесты были верны, т.к. запускались из `backend/` (cwd первый в `sys.path`), но установка тоже перенацелена.
- Превью результата в transcript сначала резало хвост, а вердикт unittest (`FAILED`/`OK`) в конце — тест это поймал; теперь head+tail.

## Открытые вопросы
- ~~`DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60`~~ — решено в Day 2: `execution_timeout_seconds` настраивается в bootstrap (`ForgeMiniSettings`).
- `TaskResponse` не содержит `status` (статус задачи — только в `forge report`: `task_status`). Добавить в публичный контракт?
- Защита тестов покрывает тестовые файлы, но не `pyproject.toml` / `setup.cfg` / `conftest.py`-подобные конфиги: агент может изменить конфигурацию запуска тестов (остаточный риск).
- Только локальный путь как источник репозитория; git URL — позже.
- `workspace_id=None` в привязке исполнения = один активный run на процесс (нормально для локального приложения).
- Образ sandbox: зависимости репозитория нужно ставить заранее (в песочнице нет сети). Позже — «setup phase» с сетью, как у Codex.
- Единый механизм выбора тестовой БД вместо 5 guard-паттернов.
- Что именно из LangGraph/approval-слоя удаляем после прототипа (ADR-003).
