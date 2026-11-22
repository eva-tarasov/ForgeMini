# ForgeMini

Рабочий прототип Forge: агент чинит код в изолированном Docker-контейнере, а результат
независимо проверяется прогоном тестов в чистом контейнере.

> Статус: прототип в разработке. Прогресс — [PROGRESS.md](PROGRESS.md), правила работы — [CLAUDE.md](CLAUDE.md).
> Исходное состояние (Forge M1, написан Codex) — тег `forge-m1-baseline`; его документация и
> прежний README — в [docs/archive/](docs/archive/) (в них относительные ссылки на старую структуру).

## Разработка
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]" -e "cli[dev]"
cd backend && python -m pytest -q      # unit, без БД
```
Интеграционные тесты: `POSTGRES_USER=... POSTGRES_PASSWORD=... scripts/ci/integration_tests.sh`
(нужен запущенный PostgreSQL и `psql`).

## Запуск прототипа
```bash
cp .env.example .env            # впишите пароль PG, OPENAI_API_KEY и FORGE_MODEL_NAME (только локально!)
docker compose up -d postgres   # PostgreSQL на 127.0.0.1:55432
cd backend && alembic upgrade head
uvicorn app.bootstrap:create_forge_app --factory --host 127.0.0.1 --port 8000
```
В другом терминале (нужен запущенный Docker):
```bash
forge solve --repo examples/demo-calc --goal "average() returns wrong values for integer lists" \
            --test "python -m unittest tests.test_average"
```
`forge solve` ждёт завершения и печатает отчёт: вердикт, доказательство (тесты **без** правки красные / **с** правкой
зелёные в чистом контейнере) и объяснение агента. Код возврата `0` — только если результат проверен независимо.
Дифф: `forge patch <run_id> > fix.patch`. Примечание: в sandbox нет сети — зависимости проекта должны быть в образе
(`--image`). Задачи для демо — [examples/demo-calc/README.md](examples/demo-calc/README.md).
