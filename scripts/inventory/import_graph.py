#!/usr/bin/env python3
"""What of backend/app is the prototype's KERNEL and what is inherited M1 weight? Measured, not guessed.

    python3 scripts/inventory/import_graph.py            # markdown report to stdout
    python3 scripts/inventory/import_graph.py --json     # machine-readable

Method: static imports (the `ast` module, so imports inside functions count too) -> a graph of app.* modules ->
reachability from the KERNEL roots below. `app.main` is special: it registers EVERY router, so a plain reachability
run from `app.bootstrap` would call the whole M1 API "used". We therefore ignore the imports made by app.main
(SEAMS) and treat only the routers the CLI really needs as roots. Everything not reachable is "M1 weight".

Limits (honest): static analysis does not see dynamic imports/getattr, and reachable != necessary. It finds
what CANNOT be needed; deciding what to delete is a human step (docs/adr/ADR-003.md).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
TESTS = ROOT / "backend" / "tests"

# What the prototype (forge solve/report/patch/run/task/workspace + sandbox + verifier) needs.
KERNEL_ROOTS = [
    "app.bootstrap",
    "app.api.agent_runs", "app.api.tasks", "app.api.workspaces", "app.api.health", "app.api.sandbox_runs",
    "app.api.error_handlers", "app.api.errors", "app.api.dependencies",
    "app.core.settings", "app.core.logging", "app.core.request_logging",
    "app.db.session", "app.db.models",
]
# THE PROPOSAL under review in ADR-003: which modules are inherited M1 weight. A pattern, not a measurement.
# The measurements below (reachability, boundary edges, tests) are what lets a human accept or amend it.
M1_RE = re.compile("|".join([
    r"app\.agent_runtime\..*",                                   # LangGraph graphs, fixture tools, demos
    r"app\.permissions(\..*)?", r"app\.tools(\..*)?",              # path permissions, approved apply_patch, registry
    r"app\.api\.(approvals|evidence)", r"app\.schemas\.(approval|evidence)",
    r"app\.domain\.(tool_call_transitions|capabilities)",
    r"app\.services\.(approval_.*|pending_approvals|local_approval_access|approved_.*|audited_tool_executor)",
    r"app\.services\.(tool_calls|tool_call_audit|repository_agent_runner|evidence_reads)",
    r"app\.services\.(verification_contract|verification_evidence|verification_observer|verification_service)",
]))

# Modules whose own imports are NOT followed: they register everything, so they hide what is really used.
SEAMS = ["app.main"]


def module_name(path: Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path, known: set[str], current: str) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    pkg = current if path.name == "__init__.py" else current.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                                           # relative import
                up = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)]
                base = ".".join([*up, base] if base else up)
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            if name in known:
                found.add(name)
            else:                                                     # `import app.x.y.z` -> nearest known prefix
                parts = name.split(".")
                while parts and ".".join(parts) not in known:
                    parts.pop()
                if parts and parts[0] == "app":
                    found.add(".".join(parts))
    return found


def build() -> tuple[dict[str, set[str]], dict[str, int]]:
    files = {module_name(p): p for p in APP.rglob("*.py")}
    known = set(files)
    graph = {m: imports_of(p, known, m) - {m} for m, p in files.items()}
    loc = {m: sum(1 for line in p.read_text().splitlines() if line.strip()) for m, p in files.items()}
    return graph, loc


def reachable(graph: dict[str, set[str]], roots: list[str], seams: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = [r for r in roots if r in graph]
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        # importing app.x.y executes the packages app and app.x too (their __init__)
        parts = m.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in graph and parent not in seen:
                stack.append(parent)
        if m not in seams:
            stack.extend(graph[m] - seen)
    return seen


def why(graph: dict[str, set[str]], target: str, roots: list[str], seams: list[str]) -> list[str] | None:
    """Shortest import chain from a kernel root to `target`: the answer to "why is this still here?"."""
    from collections import deque
    parent: dict[str, str | None] = {r: None for r in roots if r in graph}
    queue = deque(parent)
    while queue:
        m = queue.popleft()
        if m == target:
            chain, cur = [], m
            while cur is not None:
                chain.append(cur)
                cur = parent[cur]
            return chain[::-1]
        if m in seams:
            continue
        for n in sorted(graph[m]):
            if n not in parent:
                parent[n] = m
                queue.append(n)
    return None


def test_imports() -> dict[str, set[str]]:
    known = {module_name(p) for p in APP.rglob("*.py")}
    out: dict[str, set[str]] = {}
    for p in sorted(TESTS.glob("*.py")):
        out[p.name] = imports_of(p, known, "tests." + p.stem)
    return out


def package_of(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) > 2 else module


def analyse() -> dict:
    graph, loc = build()
    everything = set(graph)
    keep_now = reachable(graph, KERNEL_ROOTS, SEAMS)
    unreachable_now = sorted(everything - keep_now)
    m1 = {m for m in everything if M1_RE.fullmatch(m)}
    # the boundary: imports that go from the kernel INTO the M1 set. Each is one place to refactor before deleting.
    boundary = sorted((a, b) for a, targets in graph.items() if a not in m1 for b in targets if b in m1)
    tests = test_imports()
    classes: dict[str, list[str]] = defaultdict(list)
    for name, mods in tests.items():
        app_mods = {m for m in mods if m != "app" and not m.startswith("app.__")}
        if not app_mods:
            classes["no app imports (generic/support)"].append(name)
        elif app_mods <= m1:
            classes["only M1: deleted together with it"].append(name)
        elif app_mods & m1:
            # heuristic ONLY to order the manual triage: which half of the imports is M1?
            share = len(app_mods & m1) / len(app_mods)
            classes["mixed, mostly M1 (>=50% of imports): probably deleted, review first" if share >= 0.5
                    else "mixed, mostly kernel (<50% M1 imports): edit the M1 half out"].append(name)
        else:
            classes["kernel only: stays"].append(name)
    per_pkg: dict[str, dict[str, int]] = defaultdict(lambda: {"kernel": 0, "m1": 0})
    for m in graph:
        per_pkg[package_of(m)]["m1" if m in m1 else "kernel"] += loc[m]
    importers: dict[str, list[str]] = defaultdict(list)
    for name, mods in tests.items():
        for m in mods:
            if m in m1:
                importers[m].append(name)
    test_loc = {p.name: sum(1 for line in p.read_text().splitlines() if line.strip()) for p in TESTS.glob("*.py")}
    return {"m1": sorted(m1), "unreachable_now": unreachable_now, "boundary": boundary, "loc": loc,
            "per_pkg": dict(per_pkg), "test_classes": {k: sorted(v) for k, v in classes.items()},
            "m1_test_importers": dict(importers), "total_loc": sum(loc.values()),
            "tests_total": len(tests), "tests_loc": sum(test_loc.values()), "test_loc": test_loc}


def markdown(r: dict) -> str:
    m1_loc = sum(r["loc"][m] for m in r["m1"])
    dead_now = sum(r["loc"][m] for m in r["unreachable_now"])
    out = [f"Backend `app/`: **{r['total_loc']:,}** non-empty lines in {len(r['loc'])} modules.", "",
           f"- proposal: **{len(r['m1'])} modules / {m1_loc:,} lines ({m1_loc / r['total_loc']:.0%})** are inherited M1 weight;",
           f"- already unreachable from the kernel today (deletable right now): **{dead_now:,}** lines in "
           f"{len(r['unreachable_now'])} modules;",
           f"- the rest ({m1_loc - dead_now:,} lines) is still imported by the kernel through **{len(r['boundary'])} "
           "boundary imports** (table below): refactor first, then delete.", "",
           "| package | kernel lines | M1 lines |", "|---|---:|---:|"]
    for pkg, v in sorted(r["per_pkg"].items()):
        if v["kernel"] or v["m1"]:
            out.append(f"| `{pkg}` | {v['kernel']:,} | {v['m1']:,} |")
    out += ["", "### Boundary: kernel module -> M1 module it imports", "", "| kernel module | imports (M1) |", "|---|---|"]
    for a, b in r["boundary"]:
        out.append(f"| `{a}` | `{b}` |")
    dead = set(r["unreachable_now"])
    out += ["", "### M1 modules", "", "| module | lines | tests importing it | unreachable today |", "|---|---:|---:|:---:|"]
    for m in sorted(r["m1"], key=lambda m: (-r["loc"][m], m)):
        out.append(f"| `{m}` | {r['loc'][m]:,} | {len(r['m1_test_importers'].get(m, []))} | {'yes' if m in dead else ''} |")
    out += ["", f"### Tests ({r['tests_total']} files, {r['tests_loc']:,} lines)", "",
            "| class | files | lines |", "|---|---:|---:|"]
    for k, v in sorted(r["test_classes"].items()):
        out.append(f"| {k} | {len(v)} | {sum(r['test_loc'][f] for f in v):,} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--why", metavar="MODULE", nargs="+", help="show the import chain that keeps MODULE reachable")
    ap.add_argument("--tests", action="store_true", help="list the test files of every class (for the triage)")
    args = ap.parse_args()
    if args.why:
        g, _ = build()
        for target in args.why:
            chain = why(g, target, KERNEL_ROOTS, SEAMS)
            print(f"{target}: " + (" -> ".join(chain) if chain else "not reachable from the kernel"))
        raise SystemExit(0)
    result = analyse()
    if args.tests:
        for cls, files in sorted(result["test_classes"].items()):
            print(f"## {cls} ({len(files)})")
            for f in files:
                print(f"{result['test_loc'][f]:6}  {f}")
    else:
        print(json.dumps(result, indent=1) if args.json else markdown(result))
