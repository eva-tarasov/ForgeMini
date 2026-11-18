# Step 10A — контракт независимой проверки

## Текущий путь результата

`Task → AgentRun → AgentRunner / LangGraph → AgentFinalResult → verifying`. При claim `AgentRunService` загружает Task, собирает `AgentRunRequest` со снимком цели и упорядоченных критериев и коммитит `queued → running` до вызова runner. Для `ready_for_verification` сервис сохраняет `final_result` в JSON v1, пишет событие и переводит run в `verifying` в одной транзакции. Домен допускает ребро `verifying → succeeded`, а `transition_locked_agent_run` отклоняет переход через `VerificationEvidenceRequiredError`. [Claim и финализация](../backend/app/services/agent_runs.py#L108) · [Переход](../backend/app/services/agent_runs.py#L67) · [Codec v1](../backend/app/services/agent_result_codec.py)

## Подход и границы доверия

| Подход | Что подтверждает | Ограничение |
| --- | --- | --- |
| LLM-as-judge | Интерпретирует естественный язык и сопоставляет его с предоставленными материалами | Вывод зависит от модели и контекста. Для принятия факта о содержимом файла всё равно нужно независимое наблюдение. |
| Детерминированные проверки fixtures | Даёт воспроизводимое сравнение наблюдаемого digest с утверждённым ожидаемым digest | Критерий должен иметь явное проверяемое условие. |
| Гибрид | Позволяет использовать модель для интерпретации, сохраняя детерминированную проверку фактов | Требует явного разделения полномочий выбора проверок и вынесения итогового verdict. |

Для v1 выбран детерминированный SHA-256 конкретных файлов. Модель предлагает действия и возвращает предварительный результат. Доверенный backend связывает проверяемые критерии с файлами и ожидаемыми digest. Независимый verifier наблюдает фактическое состояние workspace. `AgentFinalResult`, вызов инструмента и запись approval описывают исполнение. Aggregator принимает типизированные наблюдения от доверенного verifier. Корреляция подтверждает принадлежность метаданных, а достоверность наблюдения остаётся ответственностью источника. [ADR-001](adr/ADR-001.md) · [ADR-002](adr/ADR-002.md) · [Граница R2-06](ARCHITECTURE_R2_REGRESSION_GATE.md#граница-step-10)

## План и наблюдения

`VerificationBinding` содержит run, task и workspace UUID и доверенный revision token fixture. `VerificationPlan` содержит ненулевой UUID плана, binding, SHA-256 каждого критерия в порядке Task и кортеж `FileDigestCheck`. Индекс критерия начинается с нуля. Check содержит UUID, индекс, логический путь файла и ожидаемый SHA-256. UUID создаёт доверенный backend. UUID нового плана выделяется при изменении состава проверок или привязки. Fingerprint вычисляется в стабильном формате v1 из binding, UUID плана, упорядоченных критериев и checks. Даже при случайном повторном использовании UUID изменение содержимого делает старые наблюдения неприменимыми.

`fingerprint_criteria()` хеширует точные UTF-8 строки уже валидированного снимка Task без повторной нормализации. Текст самих критериев в план не копируется. При построении 10B должен сверить полный упорядоченный снимок с доверенной Task. Каждый критерий обязателен и требует хотя бы одного check. Несколько checks одного критерия объединяются условием AND. Критерий, которому backend не может назначить достоверные проверки, считается unsupported. Отсутствие покрытия отклоняет план с кодом `uncovered_verification_criterion`. На этой границе вызывающий может вернуть `INCONCLUSIVE`.

Пределы v1 включают 1–10 критериев, 1–32 checks, до 16 КиБ канонических UTF-8 метаданных плана и до 128 символов ASCII в revision token. Валидация применяет общий logical path parser, защиту от запрещённых компонентов и текущий лимит глубины fixture reader. Повтор UUID или пары «критерий, путь» отклоняется. Один файл может участвовать в нескольких критериях с одинаковым ожидаемым digest. Строки SHA-256 состоят ровно из 64 строчных hex-символов. В моделях используются immutable кортежи. Пути, revision, содержимое checks и digest исключены из `repr`. Backend выбирает revision и пути из доверенного fixture setup. Логический путь в плане сам по себе не предоставляет разрешение читать файл. [Контракт](../backend/app/services/verification_contract.py) · [Logical paths](../backend/app/permissions/paths.py) · [Fixture baseline](../backend/app/tools/repository/baseline.py)

`VerificationObservationSet` связывает наблюдения с UUID, binding и fingerprint плана. Каждое `FileDigestObservation` содержит SHA-256 прочитанного файла либо явный `READ_ERROR` или `UNKNOWN_EFFECT`.

| Verdict | Условия |
| --- | --- |
| `PASS` | Корреляция совпадает, для каждого check получен ровно один digest, повторные наблюдения одного пути согласованы, все digest совпадают с ожидаемыми. |
| `FAIL` | Полный согласованный набор содержит хотя бы один подтверждённый digest, отличающийся от ожидаемого. |
| `INCONCLUSIVE` | Есть чужая корреляция, отсутствующее или лишнее наблюдение, повтор check ID, конфликт наблюдений одного пути, ошибка чтения или неизвестный эффект. Эти условия имеют приоритет над отдельным подтверждённым несовпадением. |

## Пример

```python
from hashlib import sha256
from uuid import uuid4

from app.services.verification_contract import (
    FileDigestCheck, VerificationBinding, VerificationPlan, fingerprint_criteria,
)

binding = VerificationBinding(
    run_id=uuid4(), task_id=uuid4(), workspace_id=uuid4(), baseline_revision="fixture-v1",
)
plan = VerificationPlan(
    plan_id=uuid4(),
    binding=binding,
    criterion_digests=fingerprint_criteria(("Fixture содержит утверждённый результат",)),
    checks=(FileDigestCheck(uuid4(), 0, "fixtures/output.txt", sha256(b"expected\n").hexdigest()),),
)
```

В production UUID binding подставляются из конкретных записей run, task и workspace. Ожидаемый digest задаётся из доверенной спецификации состояния, а observed digest вычисляется после независимого чтения.

## Step 10B — read-only observer

Внутренний интерфейс `await observe(plan, trusted_fixture_access) -> VerificationObservationSet`. Доверенный caller передаёт `TrustedFixtureAccess` с идентичностью выданного плана, binding, корнем fixture, независимыми read scopes и полным упорядоченным снимком критериев Task. Observer сверяет ID, fingerprint, run/task/workspace, revision и критерии до открытия корня. Тип объекта сам по себе не подтверждает полномочия caller. Задание корня, scopes и плана остаётся ответственностью доверенного backend. [Observer](../backend/app/services/verification_observer.py)

Observer создаёт отдельный `PermissionContext` с `repository.read` и вызывает `read_file` через `create_fixture_executor()`. `SafeRepositoryReader` проверяет файловые границы. Каждый уникальный логический путь читается до одного раза за вызов, а наблюдения возвращаются в порядке checks. Digest вычисляется из точных UTF-8 байт без нормализации. Отказ доступа, недоступный корень или файл, ошибка чтения, timeout и исчерпание бюджета дают `READ_ERROR`. Executor сворачивает ошибки handler в `handler_failed`, который тоже даёт `READ_ERROR`. Неожиданные ошибки протокола executor завершают наблюдение контрактной ошибкой.

Reader ограничивает файл 64 КиБ. У `read_file` также есть действующий лимит размера выходного документа с учётом имени файла. На вызов observer выделяется 256 КиБ. Перед новым чтением требуется резерв в 64 КиБ плюс один байт для проверки EOF при росте файла. При успехе списывается фактическое число байт. При отказе до handler байты не списываются. При ошибке после допуска списывается весь резерв, поскольку executor не сообщает фактический объём чтения. Пути, для которых резерва уже нет, не открываются.

```python
from app.permissions.context import PathScope
from app.services.verification_contract import aggregate_verification
from app.services.verification_observer import TrustedFixtureAccess, observe

async def verify_fixture(plan, fixture_root, task_criteria):
    access = TrustedFixtureAccess(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        binding=plan.binding,
        root=fixture_root,
        read_scopes=(PathScope("fixtures/output.txt"),),
        acceptance_criteria=task_criteria,
    )
    observations = await observe(plan, access)
    return aggregate_verification(plan, observations)
```

Отмена доставляется кооперативно между чтениями. Синхронный системный вызов может задержать и отмену, и deadline. Observer фиксирует содержимое, увиденное при каждом чтении. Согласованный snapshot нескольких файлов и отсутствие изменений после чтения остаются открытыми условиями изоляции fixture.

Для 10C доверенный оркестратор должен получить binding и критерии из действительных SQL-записей, выбрать fixture access на стороне backend, наблюдать снаружи транзакции и повторно проверить состояние run и корреляцию перед публикацией. Атомарность записи evidence и перехода состояния относится к 10C. [Текущий success gate](../backend/app/services/agent_runs.py#L67)
