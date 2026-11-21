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
