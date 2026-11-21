# ForgeMini — прогресс

Формат: коротко. Подробности и «почему» — в описаниях PR. Старая история M1 — `docs/archive/forge_progress.md`.

## Цель прототипа (v0.1-prototype, ~3 дня)
`forge run --repo <path|url> --goal "<что исправить>" --test "<команда тестов>"` →
агент чинит код в Docker-sandbox (без сети) → Verifier в чистом контейнере прогоняет тесты →
`succeeded` + проверенный diff, либо `failed` + понятное объяснение.

## Статус

| День | Шаг | Статус |
|---|---|---|
| 0 | История myFirstForge → ForgeMini, тег `forge-m1-baseline` | ветка `claude/import-history` залита; нужен rename в `main` + тег (владелец) |
| 0 | CLAUDE.md, PROGRESS.md, архив доков | в PR `claude/day0-ci-docs` |
| 0 | CI (lint / unit / integration на PG 17) | в PR `claude/day0-ci-docs` |
| 1 | DockerSandbox, tools, explicit agent loop, SandboxRunner, `examples/demo-calc` | — |
| 2 | Workspace (repo/test_command/image), Verifier, `bootstrap.py`, CLI `forge run` | — |
| 3 | Живой прогон на Mac владельца, тюнинг, тег `v0.1-prototype` | — |

## Базовая линия (для статьи: «до»)
- Unit: 1622 passed / 481 skipped (без БД). Integration (PG 16 локально): 424 passed / 29 skipped.
- CLI: 754 passed + 8 deselected (жёстко прописан путь `/Users/.../backend/.venv` в demo-launcher).
- `test_week4_live_smoke.py` исключён из CI: привязан к одной dev-БД и PostgreSQL 17.0.11.
- Ruff: правила E9+F (реальные ошибки); стиль выключен. Исправлен один неиспользуемый импорт.

## Решения
- Тесты интеграции: вместо правки ~24 файлов с 5 разными guard-паттернами имён БД — скрипт `scripts/ci/integration_tests.sh`
  создаёт БД под каждый паттерн. Дёшево и без риска сломать тесты; единый флаг — позже, при чистке.

## Открытые вопросы
- Единый механизм выбора тестовой БД вместо 5 guard-паттернов.
- Что именно из LangGraph/approval-слоя удаляем после прототипа (ADR-003).
