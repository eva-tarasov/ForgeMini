# Forge CLI v0.1 release acceptance

The owner-reviewed CLI-07 release is [PR #68](https://github.com/eva-tarasov/myFirstForge/pull/68) on `feat/week4cli-v01-release`, commit `760f8c64d75874a0ddf462299bb20fdbaf1d7b33`, based on `00d6e29fd7bf210015932180e0b782ce1a976645`. Package version stays `0.1.0`. The owner acceptance below applies to that reviewed commit.

## Current verification

CLI-07 implementation verification on Python 3.13.0 passed 7 focused release tests and 762 complete CLI tests, with 0 failures and 0 skips. The existing environment passed `pip check`, root help/version and the demo wrapper's offline help/configuration. The owner subsequently confirmed 7 release tests, 762 CLI tests and clean wheel installation PASS for reviewed commit `760f8c64d75874a0ddf462299bb20fdbaf1d7b33`. This documentation correction performs no new test execution.

The clean-install launcher builds a wheel from a disposable copy of the current CLI source and README. It installs the wheel with runtime dependencies into a fresh venv. Its cwd is outside the repository and its environment excludes PYTHONPATH and existing virtualenv configuration. Pip config files are disabled and build temporaries are scoped to the disposable directory. Package dependencies may download from the environment-configured index or pip's default index.

The initial sandbox attempt was BLOCKED at build-dependency acquisition and confirmed cleanup. A run with network permission outside the sandbox passed. Index access and IPv4 loopback permissions are requirements for this launcher. They are unnecessary for the routine offline regression suite.

The wheel probe checks package import origin, imports all production CLI modules without backend code, checks version 0.1.0, performs 25 help checks covering every group/subcommand, and runs two real HTTP health requests to a deterministic local server. It validates JSON/plain output, separated error streams, local invalid-UUID exit 2 with zero additional requests, and installed dependency consistency. All subprocesses have deadlines. Its last JSON report must contain outcome PASS and cleanup completed. Temporary wheel and venv artifacts are intentionally removed.

The latest owner clean-wheel installation passed. Its wheel SHA256 is `11feb3f5fbfe487e3e0c127d157758e5ed8a492c86ff93f3dc21a83bd6573f2f`.

The following JSON records an earlier Codex CLI-07 build, with its own wheel SHA256. Build-time metadata can change the wheel hash between builds.

```json
{"outcome":"PASS","version":"0.1.0","python":"3.13.0","wheel":"forge_cli-0.1.0-py3-none-any.whl","wheel_sha256":"2c4b7aa2318587a3f9255c04ec6741fd7c3b30f3d2306011c8a20871bd2648ef","help_checks":25,"health_requests":2,"imports_from_venv":true,"backend_imported":false,"outside_checkout":true,"source_pythonpath":false,"streams_separated":true,"invalid_input_exit":2,"dependency_check":"PASS","cleanup":"completed"}
```

That earlier build installed runtime versions Typer 0.29.1, Rich 15.0.0, HTTPX 0.28.1 and Pydantic 2.14.3. The smoke uses the wheel's declared runtime dependencies without the development extra.

## Latest owner workflow acceptance

For reviewed PR #68 commit `760f8c64d75874a0ddf462299bb20fdbaf1d7b33`, the owner repeated the real TCP/PostgreSQL four-scenario matrix and reported PASS with cleanup completed. This covers approve with one patch application and independent verification, reject, cancel and lost-response checks through the existing harness.

The owner interactive session reported SESSION_ENDED with cleanup completed and confirmed approve/reject. Personal inspection of evidence/result output in Terminal 2 was not established for this run. Automated evidence assertions belong to the TCP matrix above. Paid OpenAI smoke was not repeated in CLI-07.

## Release acceptance matrix

Historical rows retain owner-provided acceptance from the CLI-07 brief. Latest owner workflow runs have separate CLI-07 rows. Merge references identify integrated source. Precise execution SHAs are retained where supplied.

| Check | Expected behavior | Evidence source | Verified source/commit | Status | Remaining limitation |
|---|---|---|---|---|---|
| Version and packaging | 0.1.0 wheel, standalone CLI imports | CLI-07 implementation checks and latest owner clean-install PASS, [PR #68](https://github.com/eva-tarasov/myFirstForge/pull/68) | Reviewed `760f8c6` | PASS | Latest owner wheel hash is recorded above. |
| Fresh wheel installation | Dependencies installed in a fresh venv, installed CLI works outside checkout | Latest owner clean-install PASS and earlier Codex report | Reviewed `760f8c6` | PASS | Network index/loopback permissions required. Initial sandbox attempt BLOCKED. |
| Command registration and streams | 25 help checks, version, JSON/plain health, safe invalid input | Earlier Codex CLI-07 installed-wheel TCP health smoke | Reviewed `760f8c6` | PASS | Health server is deterministic, separate from the Forge application. |
| Offline compatibility | All commands, DTO/auth/error/output/polling contracts | CLI-07 implementation verification and owner confirmation, 762 passed | Reviewed `760f8c6` | PASS | MockTransport evidence does not independently repeat real workflow execution. |
| Release safeguards | Scope, environment, failed-install cleanup, wheel payload | CLI-07 implementation verification and owner confirmation, 7 passed | Reviewed `760f8c6` | PASS | Only bounded release assertions are added. |
| Dependencies | Existing and freshly installed environments consistent | CLI-07 implementation pip checks | Reviewed `760f8c6` | PASS | Download availability can vary. |
| Declared Python minimum | Python >=3.11 | pyproject, current run on 3.13.0 | `00d6e29` package declaration | NOT RUN | Python 3.11/3.12 and other OS/interpreter combinations were not exercised here. |
| CLI-01/02 foundation | Standalone Typer package and typed HTTP boundary | Merged [PR #41](https://github.com/eva-tarasov/myFirstForge/pull/41), [PR #60](https://github.com/eva-tarasov/myFirstForge/pull/60), current regression | Included in `00d6e29` | PASS | Production CLI remains HTTP-only. |
| CLI-03/04 | Workspace/Task, Rich, JSON, terminal safety | Merged [PR #62](https://github.com/eva-tarasov/myFirstForge/pull/62), [PR #65](https://github.com/eva-tarasov/myFirstForge/pull/65), current regression | Merges `d1b00ce`, `a557c6c`, included in base | PASS | Terminal observations remain separately attributed to the owner. |
| CLI-05 TCP/PostgreSQL and cancellation | Execution, results/events, active cancellation, cleanup | Owner-accepted evidence, [PR #66](https://github.com/eva-tarasov/myFirstForge/pull/66) | Integrated by `a91ca5e`; no new CLI-07 execution SHA | PASS | Not rerun in CLI-07. |
| CLI-05 live OpenAI | Bounded real provider run and independent fixture verification | Owner-accepted evidence, [PR #66](https://github.com/eva-tarasov/myFirstForge/pull/66) | Earlier live report at `1140d7a`; merged milestone `a91ca5e` | PASS | 3 requests, 1053 tokens, usage_complete true, cost unknown. No paid rerun. |
| CLI-05 Terminal | Actual owner Terminal compatibility | Owner acceptance supplied with CLI-07 brief | Merged milestone `a91ca5e`; terminal report SHA not supplied | PASS | Historical owner observation. |
| CLI-06 four-path TCP/PostgreSQL | Approve once + PASS evidence, reject, cancel, lost-response, cleanup | Historical owner-accepted evidence, [PR #67](https://github.com/eva-tarasov/myFirstForge/pull/67) | Integrated by `00d6e29`; precise historical run SHA not supplied | PASS | Original milestone acceptance retained separately. |
| CLI-06 interactive | Explicit review/approve/reject in real terminals | Owner-accepted evidence, [PR #67](https://github.com/eva-tarasov/myFirstForge/pull/67) | Merged milestone `00d6e29`; no new CLI-07 execution SHA | PASS | Visual observations are historical. |
| CLI-07 owner TCP/PostgreSQL | Four-scenario matrix PASS and cleanup completed | Latest owner acceptance, [PR #68](https://github.com/eva-tarasov/myFirstForge/pull/68) | Reviewed `760f8c6` | PASS | Existing offline-provider approval harness. |
| CLI-07 owner interactive | Explicit approve/reject, SESSION_ENDED and cleanup completed | Latest owner acceptance, [PR #68](https://github.com/eva-tarasov/myFirstForge/pull/68) | Reviewed `760f8c6` | PASS | Personal Terminal 2 evidence/result inspection was not established. |
| CLI-07 paid-provider rerun | Separate bounded live-provider smoke | No new paid execution | None | NOT RUN | Historical CLI-05 live acceptance retained. |
| Shared progress update | Reconcile CLI-01–07 without overwriting backend history | Proposed text in CLI-07 final report | Pending shared-documentation owner | NOT RUN | Root forge_progress.md retains outdated CLI-03 status. |
| Owner learning checkpoint | Explain boundary, lifecycle, approval and evidence | No checkpoint record supplied | None | NOT RUN | Owner confirmation is outstanding. |

## Demonstration and handoff

The reproducible full path uses the existing approval launcher and runtime. Its approve fixture creates Workspace, Task and AgentRun through the installed Forge CLI, executes an offline LangGraph read and patch proposal, waits for persisted approval, reviews the exact fingerprint, records explicit approval, applies once, waits for the preliminary result, then calls trusted VerificationService and reads persisted evidence. Separate fixtures cover rejection, cancellation and an unknown decision response. The current read-only preflight checks actual fixture DB identity rather than the container's bootstrap POSTGRES_DB.

The [README](README.md#local-e2e-demonstrations) contains exact opt-in commands and prerequisites for the existing TCP and owner Terminal demonstrations. The [CLI-05 harness](tests/demo_agent_run.py) remains available for its accepted execution/cancellation demonstrations. CLI-07 uses these existing runtimes unchanged.

## Technical decisions and open debt

- CLI stays HTTP-only. Backend owns policy, state, transactions and execution. Task and AgentRun remain separate concepts.
- DTOs check supported wire contracts, identities and semantic invariants. JSON bypasses Rich. Human text uses terminal-safe rendering.
- Read/review capabilities are per-request and locally validated. Review fingerprints and explicit confirmation bind approval to a particular patch. Mutating requests have no automatic retry.
- Execution uses the existing local single-process coordinator. Durable restart/resume, production authentication, generic shell execution, MCP and sandbox isolation are outside v0.1.
- Preliminary results differ from independent verification evidence. Production orchestration of verification remains future work.
- Recorded usage may be incomplete and provider cost remains unknown where accounting is absent. Technical call/token limits provide no guaranteed cost cap.
- Owner acceptance is recorded for reviewed PR #68 commit `760f8c64d75874a0ddf462299bb20fdbaf1d7b33`. Python-minimum testing, root progress reconciliation and the owner's learning checkpoint remain open handoff items. Time spent and independent backend M1 acceptance are not inferred.

## Owner evidence collection

Keep the focused/full pytest summaries, pip check, help/version output, the clean-install JSON report, current Git SHA/status/diff and this matrix together. The clean-install report includes the actual wheel hash, interpreter and runtime dependency versions. Record failures, skips and environment restrictions explicitly.

For safe failing-test diagnostics use `cli/.venv/bin/python -m pytest cli/tests/test_release.py -v -x --tb=no --show-capture=no`. The clean-install launcher emits safe stage/reason codes and omits captured installer output. A nonzero launcher exit, FAIL/BLOCKED outcome or unconfirmed cleanup does not satisfy clean-install acceptance. Ordinary test caches can remain in the checkout. Disposable build/venv/server resources are removed by the launcher, including on normal failures and interruption.
