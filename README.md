# Forge backend

Минимальный FastAPI backend для проекта Forge. Требуется Python 3.11 или новее.

Документация: [архитектура](docs/ARCHITECTURE.md), [учебное руководство](docs/LEARNING_GUIDE.md), [ADR-001](docs/adr/ADR-001.md), [backlog](backlog.md).

## Установка

Из директории `backend`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Запуск

```bash
python -m uvicorn app.main:app --reload
```

Проверка API: `http://127.0.0.1:8000/health`. Swagger UI: `http://127.0.0.1:8000/docs`.

Workspace API:

```bash
curl -i -X POST http://127.0.0.1:8000/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"name":"My Workspace"}'
curl -i http://127.0.0.1:8000/workspaces
```

Workspace хранятся только в памяти запущенного приложения и исчезают после его перезапуска.

## Локальный PostgreSQL

Для PostgreSQL 17 нужен Docker с Compose. Из корня репозитория создайте локальный файл настроек и замените шаблонный пароль своим значением. Файл `.env` игнорируется Git; `.env.example` содержит только шаблон.

```bash
cp .env.example .env
# Отредактируйте POSTGRES_PASSWORD в .env перед запуском.
docker compose config --quiet
docker compose up -d --wait
docker compose ps
```

База доступна только на `127.0.0.1:55432` (или на порту из `POSTGRES_PORT` в `.env`). Для проверки SQL используйте `psql` внутри контейнера; дополнительная установка на хосте не нужна:

```bash
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT current_database(), 1 AS ok;"'
```

Проверка сохранности данных при пересоздании контейнера:

```bash
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
CREATE TABLE IF NOT EXISTS forge_persistence_check (id integer PRIMARY KEY, marker text NOT NULL);
INSERT INTO forge_persistence_check (id, marker) VALUES (1, 'survived_restart')
ON CONFLICT (id) DO UPDATE SET marker = EXCLUDED.marker;
SELECT marker FROM forge_persistence_check WHERE id = 1;
SQL
docker compose down
docker compose up -d --wait
docker compose exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT marker FROM forge_persistence_check WHERE id = 1;"'
```

Последний `SELECT` должен вернуть `survived_restart`. Команда `docker compose down` удаляет контейнер и сеть, но сохраняет named volume `postgres_data` с данными. После проверки остановите сервис:

```bash
docker compose down
```

PostgreSQL пока не подключён к FastAPI: Workspace и Task по-прежнему хранятся в памяти. Запуск API из раздела выше не меняется.

## Ошибки API

Предусмотренные ошибки возвращаются в едином формате:

```json
{"error":{"code":"VALIDATION_ERROR","message":"Request validation failed","details":[{"field":"body.goal","message":"String is too short"}]}}
```

Для ошибок без сведений о полях `details` — пустой список. Ответы 404 и 405 сохраняют свои HTTP-статусы; ответ 405 также содержит заголовок `Allow`.

## HTTP-логи

Forge пишет одну JSON-строку в стандартный поток ошибок для каждого HTTP-запроса. Она содержит UTC-время, уровень, событие `http_request_completed`, UUID запроса, метод, шаблон маршрута, HTTP-статус и длительность в миллисекундах. Успешные и обработанные ответы содержат тот же UUID в `X-Request-ID`; при непредвиденном исключении заголовок не гарантирован. Для неизвестного маршрута в поле `route` записывается `unmatched`.

## Тесты

Из директории `backend` с активированным виртуальным окружением:

```bash
python -m pytest -q
```
