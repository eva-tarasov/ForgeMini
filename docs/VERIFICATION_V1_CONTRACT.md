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

## Step 10C — durable evidence и финализация

`VerificationService(session_factory).verify(plan, access, attempt_id=..., isolated_fixture=False)` владеет тремя фазами. Caller задаёт план и fixture access из доверенной backend composition. Идентичность попытки — пара run UUID и attempt UUID. Для повторного наблюдения после `INCONCLUSIVE` caller создаёт новый attempt UUID.

1. Фаза A открывает PostgreSQL transaction с `READ ONLY`. Сервис сверяет реальные AgentRun и Task, workspace, упорядоченные критерии, валидный предварительный result v1 со статусом `ready_for_verification` и binding плана. Новая попытка требует `verifying`. Сервис запоминает digest result и снимка audit, затем закрывает SQL session.
2. Фаза B вызывает принятый observer 10B. SQL session и row locks в этой фазе отсутствуют.
3. Фаза C получает свежий AgentRun через `FOR UPDATE`, затем защищает Task через `FOR SHARE`. Повторно проверяет binding, result, статус и audit. Сервис записывает `verification_recorded` и, при достоверном terminal verdict, status event и новый статус в одной транзакции. Чтение дочерних audit rows опирается на существующий протокол parent-first locks. Новые блокировки ToolCall или Approval не требуются, поскольку lifecycle writers сначала блокируют AgentRun. [Сервис](../backend/app/services/verification_service.py)

Success разрешает только этот узкий trusted path. Он вызывает доменную проверку перехода после оценки evidence. Общий `AgentRunService.transition_status(..., SUCCEEDED)` сохраняет `VerificationEvidenceRequiredError`. При полном audit и подтверждённой изоляции файловый `PASS` даёт `succeeded`, файловый `FAIL` даёт `failed`. При неопределённости run остаётся `verifying`. Task сохраняет собственный статус.

### Trust boundaries и freshness

`isolated_fixture=True` — отдельное подтверждение доверенного владельца fixture, который обеспечивает эксклюзивный доступ на протяжении всей операции. Параметр по умолчанию равен `False`. Без подтверждения сохраняется `INCONCLUSIVE`, даже при совпадении digest. В интеграционных тестах каждый сценарий владеет отдельным disposable root. Операторы production должны обеспечить такое же условие через lifecycle своего fixture. Передача объекта access или установка флага сама по себе не доказывает отсутствие внешних writers.

PostgreSQL обеспечивает атомарность evidence, status и status event. Файловое наблюдение имеет отдельную временную границу. Внешний writer способен изменить файл между чтением и commit. Для shared workspace с неизвестными writers данный v1 не даёт допуска к success. Автоматическая проверка exclusivity, согласованный filesystem snapshot, durable resume и механизм distributed locks требуют отдельного решения.

Evidence подтверждает наблюдавшееся состояние по доверенному плану и согласованность сохранённого audit. Metadata-only audit содержит hashes patch и sources, но не хранит patch, пути записи и результирующие bytes. Поэтому causal attribution изменения конкретного файла к конкретному handler остаётся за границей v1. Совпадение ожидаемых bytes, включая файл, который уже имел такое содержимое, позволяет подтвердить критерий состояния. Если критерий требует доказать авторство изменения, caller обязан считать его unsupported до отдельного расширения execution evidence.

### Audit gate

Сервис проверяет непрерывную последовательность AgentEvent, допустимую историю статусов от `queued` до `verifying`, ровно одно событие ready-result и отсутствие execution failure. Каждый ToolCall должен принадлежать run, иметь известный fixture tool v1, состояние `succeeded` и ровно три согласованных события proposed → started → finished до preliminary result. Оба audit schema versions и tool version проверяются с точными JSON-типами. Чужие, дублированные, неизвестные или отсутствующие lifecycle events закрывают terminal admission.

Для `apply_patch` требуется ровно одно approved Approval с `decided_at`, точными requested/decided events, workspace, ToolCall identity, tool version, hashes patch/sources и порядком proposal → request → decision → start → finish. Read tools не должны иметь Approval. `proposed`, `running`, `failed`, `denied`, pending/rejected approval и `patch_partial_apply` оставляют итог `INCONCLUSIVE`. Подтверждённое несовпадение файла также не даёт terminal decision при неполном audit.

Отсутствие ToolCall допустимо, если execution не заявлял audited operations и история run/result полная. Это позволяет проверить исходное состояние fixture. Сырые LLM-ответы не задают план и observed digest. Бюджет audit составляет 32 ToolCall, 32 Approval и 512 AgentEvent на run. Превышение даёт `INCONCLUSIVE`. Изменение снимка audit между фазами A и C также даёт `INCONCLUSIVE`, даже если восстановленные данные выглядят полными.

### Event contract v1

`verification_recorded` использует существующую AgentEvent JSONB колонку. Payload ограничен 32 КиБ UTF-8. Он содержит следующие поля.

| Поле | Содержимое |
| --- | --- |
| `schema_version`, `attempt_id` | Версия 1 и ненулевой UUID попытки |
| `plan` | Run/task/workspace UUID, revision, UUID и fingerprint плана, ordered criterion digests, check UUID, index, logical path и expected SHA-256 |
| `observations` | Check UUID и observed SHA-256 либо закрытый issue |
| `audit_complete`, `isolated_fixture` | Результат audit gate и подтверждение caller |
| `audit_sha256`, `result_sha256`, `access_sha256` | Корреляция audit, preliminary result и выбранных root/scopes |
| `file_verdict`, `verdict` | Результат 10A и итог допуска |

Host root и scopes входят только в digest access. Содержимое файлов, credentials, raw patch, Task text и preliminary summary не копируются в evidence. Decoder заново строит immutable модели 10A, сверяет fingerprint, вычисляет оба verdict и проверяет точное соответствие каноническому payload. [Codec](../backend/app/services/verification_evidence.py)

### Конкуренция и сбои

AgentRun row lock сериализует terminal commit с cancel и другим verifier. Если cancellation закоммичена первой, verifier отклоняет устаревший статус. Если terminal verification закоммичена первой, поздняя cancellation получает недопустимый terminal transition. Терминальный run принимает только replay уже записанной попытки. Новая попытка не переоткрывает его.

Replay той же identity с тем же планом, access digest и isolation возвращает прежнее evidence без повторного чтения. Изменённый запрос даёт `verification_attempt_conflict`. Две попытки с одной identity создают одно evidence. Для разных identity первый terminal commit закрывает допуск второму verifier. Повторные события с одинаковой identity считаются ошибкой `duplicate_verification_evidence`. Уникальность обеспечивается прикладным parent lock при записи через сервис. Прямой SQL write за пределами trusted приложения способен нарушить этот протокол.

Ошибки записи evidence, transition, status event, SQL constraint и commit до подтверждённого завершения транзакции приводят к rollback всех staged writes. Cooperative cancellation пробрасывается и запускает rollback. Потеря подтверждения уже выполненного COMMIT имеет неопределённый для caller исход. В таком случае caller перечитывает durable попытку или повторяет тот же attempt UUID. Тест с потерянным подтверждением показывает один terminal result и один evidence после replay. Безопасный replay не повторяет write.

### Архитектурные решения

| Решение | Статус | Основание и граница |
| --- | --- | --- |
| Application service, PostgreSQL source of truth, parent-first locks | KEEP | ADR-001/002 и текущий lifecycle протокол |
| Evidence в существующем AgentEvent JSONB, три короткие фазы | ADOPT | Обеспечивает атомарную финализацию без новой persisted schema |
| Изолированный disposable fixture и отдельное подтверждение владельца | ADOPT | Минимальная явно ограниченная область v1 |
| Отдельная evidence table с database uniqueness | DEFER | Потребует migrations и согласования владельца при выходе за текущий write protocol |
| Доказательство авторства и patch-to-path/result correlation | SPIKE | Нужен отдельный контракт audit для таких критериев |
| Shared workspace snapshots, durable orchestration, distributed locks | DEFER | Требуют отдельного Change Gate |
