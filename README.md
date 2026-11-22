# ForgeMini

Рабочий прототип Forge: агент чинит код в изолированном Docker-контейнере, а результат
независимо проверяется прогоном тестов в чистом контейнере.

> Статус: прототип в разработке. Прогресс — [PROGRESS.md](PROGRESS.md), правила работы — [CLAUDE.md](CLAUDE.md).
> Исходное состояние (Forge M1, написан Codex) — тег `forge-m1-baseline`; его документация и прежний README остаются
> в истории git (в дереве их нет: описывали другую структуру кода).

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

**Репозиторий — путь или git URL.** `--repo https://github.com/owner/repo` (или `git@github.com:owner/repo.git`): CLI сам
делает `git clone --depth 1` в `~/.forgemini/clones/<имя>-<хеш>-<время>` и работает с этой копией (она остаётся: Verifier
перечитывает исходники). Отклоняются URL с логином/паролем, `http://`, экзотические протоколы и всё, что начинается с `-`.
Приватный репозиторий клонируйте сами (ваш SSH-ключ) и передайте путь.
**`--apply`**: если результат проверен, патч сразу применяется к репозиторию (`git apply`, всё-или-ничего; не проверен — не применяется).

## Демо: чужой репозиторий → баг → ForgeMini → проверенное исправление
```bash
scripts/demo/run.sh              # баг №1, пауза по Enter между сценами (для живого показа)
scripts/demo/run.sh --issue 2    # баг №2 или №3
scripts/demo/run.sh --all        # все три бага за один прогон (весь набор тестов)
scripts/demo/run.sh --auto       # паузы по таймеру: для записи экрана / asciinema
```
Нужен запущенный сервер (терминал 1) и активированный venv. Демо строит «чужой» репозиторий-библиотеку цен
(`examples/forge-demo-shop`, 3 заложенных бага, красные регрессионные тесты в последнем коммите) и показывает пять сцен:
репозиторий → баг-репорт и красные тесты → `forge solve` → дифф (тесты не тронуты) → зелёные тесты.
Чтобы цель была настоящим репозиторием на GitHub: `scripts/demo/make_demo_repo.sh /tmp/shop && cd /tmp/shop && gh repo create forge-demo-shop --private --source=. --push`,
затем `DEMO_REPO_URL=https://github.com/<вы>/forge-demo-shop scripts/demo/run.sh`.

### Если что-то не работает
| Симптом | Причина → что делать |
|---|---|
| `zsh: command not found: python` / `pip` | На macOS до активации venv есть только `python3`. Используйте `python3 -m venv .venv`, после `source .venv/bin/activate` `python` и `pip` появятся |
| `zsh: command not found: forge` | В этом окне не активирован venv: `source .venv/bin/activate` |
| `Cannot reach the Forge server ...` | Сервер не запущен: в другом терминале `scripts/start.sh` и дождитесь `Uvicorn running` |
| `ModuleNotFoundError: langchain_core` и в трейсбеке `/Library/Frameworks/Python.framework/...` | Запущен глобальный `uvicorn`/`alembic`, не из venv, либо зависимости не установлены: `pip install -e "backend[dev]" -e "cli[dev]"` в активном venv. `scripts/start.sh` всегда использует интерпретатор venv |
| `wrong database password` | Том БД помнит пароль первого запуска. Запустите `scripts/setup.sh`: он предложит сбросить старую локальную БД (удаляется только локальная БД ForgeMini) |
| `POST /agent-runs/{id}/start` → 503 | Нет `OPENAI_API_KEY` / `FORGE_MODEL_NAME` (повторно: `scripts/setup.sh`), либо Docker недоступен. Текст ответа называет причину |
| `--repo is not a directory and not a git URL` | Путь считается от текущего каталога: запускайте из корня репозитория или дайте абсолютный путь |
| `git clone failed` | Репозиторий приватный или URL неверный. Склонируйте сами (SSH-ключ / credential helper) и передайте путь |
| `Not applied: ... git apply --check failed` (код 7) | После запуска вы изменили те же места. Патч не применён, дерево не тронуто; применить вручную: `forge patch <run_id> \| git apply` |
| Долгий первый запуск | Скачивается образ sandbox (`python:3.12-slim`); `start.sh` делает это заранее |
