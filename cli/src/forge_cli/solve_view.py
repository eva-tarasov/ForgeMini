"""The "show, don't tell" terminal view of `forge solve`: what we took, what the agent did, what was proven.

Used only on an interactive terminal; pipes, `--json` and CI keep the plain line-by-line output.
Everything that came from the server (model text, test output, paths) is untrusted: it is stripped of
control characters and printed as `Text` objects, never as rich markup (so "[red]" in a file name
cannot restyle or hide anything).
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from forge_cli.dto import RunReport, VerificationView
from forge_cli.solve import cost_text, next_step_lines, sanitize

MAX_DIFF_LINES = 70
ARROW = "▸"


def _t(value: str, limit: int = 200, style: str = "") -> Text:
    return Text(sanitize(value, limit).replace("\n", " "), style=style)


def colorize_diff(diff: str, *, max_lines: int = MAX_DIFF_LINES) -> Text:
    out = Text()
    lines = sanitize(diff, 200_000).splitlines()
    for line in lines[:max_lines]:
        if line.startswith(("diff ", "index ", "--- ", "+++ ", "new file", "deleted file")):
            style = "bold"
        elif line.startswith("@@"):
            style = "cyan"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        else:
            style = "dim"
        out.append(line[:160] + "\n", style=style)
    if len(lines) > max_lines:
        out.append(f"… {len(lines) - max_lines} more lines (forge patch RUN_ID shows all)\n", style="dim italic")
    return out


class SolveView:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._t0 = time.monotonic()
        self._announced_verifying = False

    # -- before the run -------------------------------------------------------------------------
    def banner(self, *, repo_label: str, commit: str | None, goal: str, test: str) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", justify="right")
        grid.add_column()
        grid.add_row("repository", _t(repo_label + (f"  @ {commit}" if commit else ""), 300))
        grid.add_row("task", _t(goal, 500))
        grid.add_row("tests", _t(test, 300, "yellow"))
        self.console.print(Panel(grid, title=Text("ForgeMini", style="bold"),
                                 subtitle=Text("agent in a sandbox without network · verified by an independent re-run",
                                               style="dim"), border_style="cyan"))

    def note(self, text: str) -> None:
        for line in sanitize(text, 600).splitlines() or [""]:
            self.console.print(Text("  " + line, style="dim"))

    def started(self) -> None:
        self.console.print(Rule(Text("agent at work", style="bold"), style="cyan"))

    # -- while it runs --------------------------------------------------------------------------
    def event(self, event) -> None:
        kind = event.kind
        if kind == "agent_run_status_changed" and event.details.to == "verifying" and not self._announced_verifying:
            self._announced_verifying = True
            self.console.print(Rule(Text("verification", style="bold"), style="cyan"))
            self.console.print(Text(f"  {ARROW} the agent only CLAIMED it is done; Forge now re-runs the tests "
                                    "itself, in a fresh container", style="cyan"))
            return
        if kind != "agent_step":
            return
        d = event.details
        if d.step == "tool_result" and d.tool == "finish":
            return                                      # the "agent claims…" line below says it better
        if d.step == "tool_result" and d.tool:
            self.console.print(self._tool_row(d))
        elif d.step == "model_response" and d.tools is not None and not d.tools:
            self.console.print(Text("  … the model answered without calling a tool", style="dim"))
        elif d.step == "model_response" and d.tools is None:
            self.console.print(_t("  " + d.summary, 220, "dim"))
        elif d.step == "finish":
            claim = "claims it is done" if d.outcome == "claimed_done" else "gives up: cannot fix"
            self.console.print(Text(f"  ■ agent {claim}", style="bold " + ("green" if d.outcome == "claimed_done" else "yellow")))
        elif d.step in ("budget_exhausted", "error", "other"):
            self.console.print(_t("  ! " + d.summary, 220, "yellow"))

    @staticmethod
    def _tool_row(d) -> Text:
        row = Text("  ")
        if d.tool == "bash":
            if not d.ok:
                row.append("✗ ", style="red")
            elif d.exit_code == 0:
                row.append("✓ ", style="green")
            else:
                row.append("● ", style="yellow")
            row.append("$ ", style="bold")
            row.append(sanitize(d.target or "", 90).replace("\n", " ⏎ "))
            if d.exit_code is not None:
                row.append(f"   exit {d.exit_code}", style="green" if d.exit_code == 0 else "red")
        elif d.tool == "read_file":
            row.append("◦ read  ", style="blue")
            row.append(sanitize(d.target or "", 90))
        elif d.tool == "edit_file":
            row.append(("✎ edit  " if d.ok else "✗ edit  "), style="magenta" if d.ok else "red")
            row.append(sanitize(d.target or "", 90))
            if not d.ok:
                row.append("   (rejected, the model sees the error and retries)", style="dim")
        else:
            row.append(f"· {sanitize(d.tool or '', 40)}", style="dim")
        return row

    # -- after the run --------------------------------------------------------------------------
    def verdict(self, report: RunReport, run_id: str, diff: str | None) -> None:
        verified = report.run_status == "succeeded"
        v = report.verification
        parts: list = []
        if v is not None:
            parts.append(self._checks(v))
        if diff:
            parts += [Text(""), Text("The patch (what the agent changed):", style="bold"), colorize_diff(diff)]
        if report.agent_summary:
            parts += [Text("Agent says:", style="bold"), _t(report.agent_summary, 1200)]
        if report.blocked_reason:
            parts += [Text("Why it stopped:", style="bold"), _t(report.blocked_reason, 1200)]
        self.console.print(Rule(Text("result", style="bold"), style="green" if verified else "red"))
        self.console.print(Group(*parts))
        self.console.print(self._result_panel(report, run_id, verified))
        if not verified:
            for line in next_step_lines(report, run_id):
                self.console.print(_t(line, 400, "yellow"))

    def _checks(self, v: VerificationView) -> Table:
        table = Table(box=None, show_header=False, padding=(0, 2), title="Independent verification",
                      title_style="bold", title_justify="left")
        table.add_column(width=2)
        table.add_column()
        table.add_column(style="dim")
        base_ok = v.baseline is not None and v.baseline.exit_code != 0
        patched_ok = v.patched is not None and v.patched.exit_code == 0 and not v.patched.timed_out
        untouched = not v.protected_violations
        table.add_row(*self._mark(base_ok, "Tests on the original code"),
                      (f"exit {v.baseline.exit_code}, expected: fail (there was a bug)" if v.baseline else "not run"))
        table.add_row(*self._mark(patched_ok, "Tests with the patch, fresh container"),
                      (f"exit {v.patched.exit_code}, expected: pass" if v.patched else "not run"))
        table.add_row(*self._mark(untouched, "Test files untouched"),
                      "expected: the agent may not edit tests" if untouched else
                      "changed: " + ", ".join(sanitize(p, 80) for p in v.protected_violations))
        if v.changed_paths:
            table.add_row("", Text("Files changed"), Text(", ".join(sanitize(p, 80) for p in v.changed_paths)))
        return table

    @staticmethod
    def _mark(ok: bool, label: str) -> Iterable[Text]:
        return (Text("✓" if ok else "✗", style="bold green" if ok else "bold red"), Text(label))

    def _result_panel(self, report: RunReport, run_id: str, verified: bool) -> Panel:
        took = time.monotonic() - self._t0
        head = Text()
        head.append("VERIFIED" if verified else f"NOT VERIFIED ({report.run_status})",
                    style="bold green" if verified else "bold red")
        if report.usage is not None:
            u = report.usage
            head.append(f"   {u.steps} model calls · {u.tool_calls} tool calls", style="dim")
            if u.input_tokens or u.output_tokens:
                head.append(f" · {u.input_tokens:,} in / {u.output_tokens:,} out tokens", style="dim")
            if u.cost_status in ("estimated", "incomplete") and u.known_cost_usd is not None:
                head.append(f" · {cost_text(u)}", style="dim")
        head.append(f" · {took:.0f}s", style="dim")
        lines: list = [head]
        if report.has_patch:
            lines.append(Text(f"forge patch {run_id} | git apply", style="cyan"))
        return Panel(Group(*lines), border_style="green" if verified else "red")
