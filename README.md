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
Нужны: **Docker Desktop (запущен)** и Python 3.11+. На macOS команда называется `python3`. Все команды — из корня репозитория.

**Один раз:**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]" -e "cli[dev]"
scripts/setup.sh          # создаёт .env: сам генерирует пароль PG, спрашивает OPENAI_API_KEY (ввод скрыт) и имя модели
```
`setup.sh` — единственное место, где вводится ключ; он попадает только в локальный `.env` (его нет в git). `.env` руками править не нужно.

**Каждый раз — два терминала:**

| Терминал 1 (сервер, не закрывать) | Терминал 2 (команды) |
|---|---|
| `source .venv/bin/activate`<br>`scripts/start.sh` | `source .venv/bin/activate` ← в каждом новом окне!<br>`forge solve --repo examples/demo-calc --goal "average() returns wrong values for integer lists" --test "python -m unittest tests.test_average"` |

Сервер готов, когда в терминале 1 появилось `Uvicorn running on http://127.0.0.1:8000`.

`scripts/start.sh` перед стартом проверяет окружение и при проблеме пишет одну конкретную причину и что делать.
`forge solve` ждёт завершения и печатает отчёт: вердикт, доказательство (тесты **без** правки красные / **с** правкой
зелёные в чистом контейнере) и объяснение агента. Код возврата `0` — только если результат проверен независимо.
Дифф: `forge patch <run_id> > fix.patch`. В sandbox нет сети — зависимости проекта должны быть в образе (`--image`).
Задачи для демо — [examples/demo-calc/README.md](examples/demo-calc/README.md).

### Если что-то не работает
| Симптом | Причина → что делать |
|---|---|
| `zsh: command not found: python` / `pip` | На macOS до активации venv есть только `python3`. Используйте `python3 -m venv .venv`, после `source .venv/bin/activate` `python` и `pip` появятся |
| `zsh: command not found: forge` | В этом окне не активирован venv: `source .venv/bin/activate` |
| `Cannot reach the Forge server ...` | Сервер не запущен: в другом терминале `scripts/start.sh` и дождитесь `Uvicorn running` |
| `ModuleNotFoundError: langchain_core` и в трейсбеке `/Library/Frameworks/Python.framework/...` | Запущен глобальный `uvicorn`/`alembic`, не из venv, либо зависимости не установлены: `pip install -e "backend[dev]" -e "cli[dev]"` в активном venv. `scripts/start.sh` всегда использует интерпретатор venv |
| `wrong database password` | Том БД помнит пароль первого запуска. Запустите `scripts/setup.sh`: он предложит сбросить старую локальную БД (удаляется только локальная БД ForgeMini) |
| `POST /agent-runs/{id}/start` → 503 | Нет `OPENAI_API_KEY` / `FORGE_MODEL_NAME` (повторно: `scripts/setup.sh`), либо Docker недоступен. Текст ответа называет причину |
| `--repo is not a directory` | Путь считается от текущего каталога: запускайте из корня репозитория или дайте абсолютный путь |
| Долгий первый запуск | Скачивается образ sandbox (`python:3.12-slim`); `start.sh` делает это заранее |
