"""python -m evalkit {list,run,report}  (use scripts/eval.sh: it sets PYTHONPATH)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from forge_cli.client import ForgeAPIClient
from forge_cli.config import resolve_base_url
from forge_cli.errors import ForgeTransportError

from evalkit.model import Attempt
from evalkit.report import render
from evalkit.runner import copy_repo, run_attempt
from evalkit.tasks import ROOT, TASKS, by_id

DEFAULT_OUT = ROOT / "evals" / "results"


def _commit() -> str:
    done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                          check=False)
    return done.stdout.strip() or "unknown"


def _save(path: Path, meta: dict, attempts: list[Attempt]) -> None:
    path.write_text(json.dumps({"meta": meta, "attempts": [a.to_json() for a in attempts]}, indent=2) + "\n")


def cmd_list(_args) -> int:
    for t in TASKS:
        print(f"{t.id:28} {t.expect:7} {t.title}")
    return 0


def cmd_run(args) -> int:
    try:
        tasks = by_id([t for t in args.tasks.split(",") if t] if args.tasks else None)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    total = len(tasks) * args.runs
    print(f"Plan: {len(tasks)} tasks x {args.runs} runs = {total} attempts against {args.base_url}")
    print("Every attempt makes real model calls through the server (the API key from the SERVER's .env).")
    print("Cost depends on the model; the final report prints the actual token counts. Use --tasks / --runs to shrink it.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to confirm.", file=sys.stderr)
            return 2
        if input("Continue? [y/N] ").strip().lower() != "y":
            return 1

    started = datetime.now(UTC)
    meta = {"started_at": started.strftime("%Y-%m-%d %H:%M UTC"), "model": args.model, "runs": args.runs,
            "commit": _commit(), "base_url": args.base_url}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = started.strftime("%Y%m%dT%H%M%SZ")
    attempts: list[Attempt] = []

    with ForgeAPIClient(args.base_url) as client, tempfile.TemporaryDirectory(prefix="forge-eval-") as tmp:
        try:
            client.get_health()
        except ForgeTransportError:
            print(f"Cannot reach the Forge server at {args.base_url}. Start it: scripts/start.sh", file=sys.stderr)
            return 3
        sources = {t.id: copy_repo(t, Path(tmp) / t.id) for t in tasks}
        try:
            for n in range(1, args.runs + 1):                 # round-robin: a drift in time hits every task alike
                for task in tasks:
                    a = run_attempt(client, task, n, source=sources[task.id], timeout=args.timeout)
                    attempts.append(a)
                    _save(out_dir / f"{stem}.json", meta, attempts)          # a crash must not lose finished runs
                    tag = "  <-- FALSE SUCCESS" if a.false_success else ""
                    print(f"[{len(attempts)}/{total}] {task.id} #{n}: {a.outcome}"
                          f"{'' if a.oracle == 'n/a' else f' (oracle {a.oracle})'}, {a.steps} steps, "
                          f"{a.input_tokens + a.output_tokens:,} tokens, {a.seconds:.0f}s{tag}", flush=True)
        except KeyboardInterrupt:
            print("\nInterrupted: reporting what finished so far.")

    if not attempts:
        return 1
    text = render(meta, attempts)
    (out_dir / f"{stem}.md").write_text(text)
    print("\n" + text)
    print(f"Saved: {out_dir / (stem + '.json')}  and  {out_dir / (stem + '.md')}")
    return 0 if not any(a.false_success for a in attempts) else 4


def cmd_report(args) -> int:
    data = json.loads(Path(args.file).read_text())
    attempts = [Attempt(**a) for a in data["attempts"]]
    print(render(data["meta"], attempts))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evalkit", description="ForgeMini mini-evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show the tasks").set_defaults(fn=cmd_list)
    r = sub.add_parser("run", help="run the tasks N times against a running server")
    r.add_argument("--runs", type=int, default=3, help="attempts per task (default 3)")
    r.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    r.add_argument("--base-url", default=None, help="Forge server (default FORGE_BASE_URL or http://127.0.0.1:8000)")
    r.add_argument("--timeout", type=int, default=900, help="seconds per attempt")
    r.add_argument("--model", default="unknown", help="label recorded in the report (e.g. the FORGE_MODEL_NAME you use)")
    r.add_argument("--out", default=str(DEFAULT_OUT), help="where to write <timestamp>.json / .md")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    r.set_defaults(fn=cmd_run)
    rep = sub.add_parser("report", help="re-render a saved .json result as markdown")
    rep.add_argument("file")
    rep.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    if args.cmd == "run":
        if args.runs < 1 or args.runs > 20:
            p.error("--runs must be 1..20")
        args.base_url = resolve_base_url(args.base_url)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
