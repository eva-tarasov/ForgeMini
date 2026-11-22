# ForgeMini — инструкции для Claude

## Что это
ForgeMini — рабочий прототип «Forge»: даём репозиторий + задачу + команду тестов, агент чинит код
в одноразовом Docker-контейнере без сети, затем Forge **сам** повторно прогоняет тесты в чистом
контейнере и отдаёт проверенный diff (или объяснение, почему не вышло).
Исходная кодовая база — `eva-tarasov/myFirstForge`, написана Codex; состояние на момент форка — коммит `e5d6377`
(старые доки удалены из дерева, остаются в истории git: `git show e5d6377:docs/...`). Тега нет — решение владельца.
Репозиторий публичный: `eva-tarasov/ForgeMini`, история переписана (без личного e-mail и путей). Разработка до публикации —
в приватном архиве `eva-tarasov/ForgeMini-dev`: номера PR в истории до «G, часть 2» (#1–#20) относятся к нему, а «(#NN)» в
коммитах M1 — к `myFirstForge`.

Владелец: Evgenii, senior iOS-разработчик, цель — стать AI Product Engineer. Параллельно учит Python backend.
Код пишет Claude, решения/ревью/merge — владелец. Репозиторий — основа будущей статьи (Habr-формат):
фиксируй в `PROGRESS.md` не только «что сделано», но и **почему** (развилки, ошибки, неожиданности).

## Как общаться с владельцем
- По-русски, английские термины при первом упоминании поясняй.
- Объясняй «как работает под капотом»; если владелец неправ — скажи прямо.
- Не переспрашивай очевидное: прими разумное допущение и явно его назови.

## Правила работы
1. **Никаких прямых push в `main`.** Один шаг = одна ветка `claude/<шаг>` = один PR. В описании PR: что и зачем, как проверить, что осталось.
2. **API-ключи никогда не просим и не принимаем в чате.** Живой LLM запускает владелец у себя, ключ только в его локальном `.env` (в git не попадает).
3. Тест, который «зелёный», потому что ничего не проверяет, — дефект. Критерий успеха задачи (`succeeded`) выставляет только Verifier с evidence, никогда агент.
4. Каждый шаг заканчивается проверкой: тесты запущены и прошли, а не «должны пройти». Если что-то не удалось запустить — так и пишем.
5. После каждого PR обновляй `PROGRESS.md` (коротко: статус + решения + открытые вопросы).

## Карта кода
- `backend/app/` — FastAPI-монолит: `domain/` (state machines Task/AgentRun), `services/`, `repositories/`, `api/`, `sandbox/` (Docker-песочница, цикл агента, Verifier), `db/`.
- `backend/alembic/` — миграции. `cli/` — `forge-cli` (Typer, ходит только по HTTP).
- Унаследованный код M1 удалён целиком (ADR-003, C1–C5): слой approvals/evidence, fixture-инструменты, path-permissions, LangGraph/`agent_runtime/`, таблицы `tool_calls`/`approvals`/`artifacts`, статус `waiting_approval`. Из LangChain остались только `langchain-core` (типы сообщений) и `langchain-openai` (модель). Храповик `test_inventory_tool.py` не даст вернуть импорт ядро→M1.
- Ядро: domain/state machines, репозитории, `AgentService.execute_run`, учёт стоимости, public API, CLI.

- `evals/` — мини-evals (`scripts/eval.sh`): задачи, скрытые оракулы, раннер, отчёт. Метрика №1 — ложные успехи.

## Команды
```bash
# Unit-тесты (без БД; интеграционные скипаются)
cd backend && python -m pytest -q
# Интеграционные: сами создадут нужные БД forge_*_ci и мигрируют их (включая тест миграции C5 вниз/вверх)
POSTGRES_USER=forge POSTGRES_PASSWORD=... scripts/ci/integration_tests.sh
# Lint
ruff check backend cli
# Покрытие (greenlet включён в pyproject: без него теряются строки после await)
cd backend && python -m pytest -q --cov=app --cov-report=json:cov.json tests
python scripts/inventory/coverage_diff.py before.json after.json   # регрессия = больше непокрытых строк
```
CI: `.github/workflows/ci.yml` (lint, unit, integration на postgres:17).

## Известные ограничения окружения Claude
Облачная среда без доступа к OpenAI/Docker Hub: Docker есть (образ собираем локально), LLM — только
сценарная фейковая модель. Живой прогон — на машине владельца.
