# ForgeMini — прогресс

Формат: коротко. Подробности и «почему» — в описаниях PR. Старая история M1 — `docs/archive/forge_progress.md`.

## Цель прототипа (v0.1-prototype, ~3 дня)
`forge run --repo <path|url> --goal "<что исправить>" --test "<команда тестов>"` →
агент чинит код в Docker-sandbox (без сети) → Verifier в чистом контейнере прогоняет тесты →
`succeeded` + проверенный diff, либо `failed` + понятное объяснение.

## Статус

| День | Шаг | Статус |
|---|---|---|
| 0 | История myFirstForge → ForgeMini, тег `forge-m1-baseline` | история в `claude/main` (push в `main`/теги из сессии запрещены прокси); **тег ещё не поставлен — владельцу** |
| 0 | CLAUDE.md, PROGRESS.md, архив доков | ✅ PR #1 |
| 0 | CI (lint / unit / integration на PG 17) | ✅ PR #1 |
| 1 | DockerSandbox, tools, explicit agent loop, SandboxRunner, `examples/demo-calc` | ✅ PR «Day 1» (на ревью) |
| 2 | Workspace (repo/test_command/image), Verifier, `bootstrap.py`, CLI `forge run` | — |
| 3 | Живой прогон на Mac владельца, тюнинг, тег `v0.1-prototype` | — |

## Базовая линия (для статьи: «до»)
- Unit: 1622 passed / 481 skipped (без БД) на момент форка; после Day 1: 1669 passed (+47 тестов sandbox). Integration (PG 16 локально): 424 passed / 29 skipped.
- CLI: 754 passed + 8 deselected (жёстко прописан путь `/Users/.../backend/.venv` в demo-launcher).
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

## Находки по ходу (для статьи)
- Мой первый тест на «нет сети» был бы зелёным и без изоляции: в облачной среде интернета нет вообще. Заменён на проверку «в контейнере только интерфейс `lo`»; проверено, что без `--network none` появляется `eth0`.
- Мутационная проверка ключевого теста (модель видит ошибки инструментов): сломал код — тест упал, вернул — зелёный.
- Превью результата в transcript сначала резало хвост, а вердикт unittest (`FAILED`/`OK`) в конце — тест это поймал; теперь head+tail.

## Открытые вопросы
- `AgentService.DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60` — для агента с Docker мало; сделать настраиваемым (день 2).
- Образ sandbox: зависимости репозитория нужно ставить заранее (в песочнице нет сети). Позже — «setup phase» с сетью, как у Codex.
- Единый механизм выбора тестовой БД вместо 5 guard-паттернов.
- Что именно из LangGraph/approval-слоя удаляем после прототипа (ADR-003).
