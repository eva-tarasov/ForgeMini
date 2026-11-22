#!/usr/bin/env python3
"""Did deleting tests lose protection of the code that STAYS? Compare two `coverage json` reports module by module.

    coverage json --data-file=BEFORE -o before.json ; ... ; coverage json --data-file=AFTER -o after.json
    python3 scripts/inventory/coverage_diff.py before.json after.json

A module regresses when it has MORE uncovered statements than before: that is new unprotected code, which is exactly
what a wrongly deleted test produces. The PERCENTAGE is printed but never decides: cutting a covered M1 branch out of a
kernel module lowers its percentage with nothing lost (a rule on percent would raise false alarms in exactly the
cleanup this tool exists for). Modules that disappeared are listed (deleted code, nothing left to protect).
Exit code 1 if any module regressed.
"""
from __future__ import annotations

import json
import sys


def load(path: str) -> dict[str, tuple[int, int]]:
    """module path -> (statements, missing)"""
    files = json.load(open(path))["files"]
    return {f: (v["summary"]["num_statements"], v["summary"]["missing_lines"]) for f, v in files.items()}


def compare(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> dict[str, list]:
    regressed, gone, new = [], [], []
    for f, (n0, miss0) in sorted(before.items()):
        if f not in after:
            gone.append(f)
            continue
        n1, miss1 = after[f]
        pct0 = 100 * (n0 - miss0) / n0 if n0 else 100.0
        pct1 = 100 * (n1 - miss1) / n1 if n1 else 100.0
        if miss1 > miss0:
            regressed.append((f, miss0, miss1, round(pct0, 1), round(pct1, 1)))
    new = sorted(set(after) - set(before))
    return {"regressed": regressed, "gone": gone, "new": new}


def main(argv: list[str]) -> int:
    before, after = load(argv[1]), load(argv[2])
    r = compare(before, after)
    print(f"modules before: {len(before)}, after: {len(after)}; deleted: {len(r['gone'])}; new: {len(r['new'])}")
    for f, m0, m1, p0, p1 in r["regressed"]:
        print(f"REGRESSED {f}: uncovered {m0} -> {m1}, coverage {p0}% -> {p1}%")
    if not r["regressed"]:
        print("no module that exists in both reports lost coverage")
    return 1 if r["regressed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
