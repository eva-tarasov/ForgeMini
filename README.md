# Forge backend

Минимальный FastAPI backend для проекта Forge. Требуется Python 3.11 или новее.

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

## Тесты

Из директории `backend` с активированным виртуальным окружением:

```bash
python -m pytest -q
```
