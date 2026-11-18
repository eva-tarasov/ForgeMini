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

## Граница 10B

Предлагаемый внутренний интерфейс `observe(plan, fixture_binding) -> VerificationObservationSet`. `fixture_binding` принадлежит backend и задаёт доверенный корень, revision и существующий read scope. Реализация должна сверить run/task/workspace и полный снимок критериев, читать через `SafeRepositoryReader`, применять бюджеты и возвращать только digest или закрытый код неопределённости. При отсутствии достоверного наблюдения критерий получает `INCONCLUSIVE`. Успешное сравнение ограничено перечисленными файлами и наблюдавшимся временем чтения. Snapshot между файлами, freshness и атомарная публикация evidence со сменой состояния требуют отдельного контракта. [Fixture filesystem](../backend/app/tools/repository/filesystem.py#L81)
