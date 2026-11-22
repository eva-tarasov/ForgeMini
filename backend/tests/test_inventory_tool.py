"""scripts/inventory/import_graph.py on a tiny synthetic project: the logic ADR-003's numbers rest on."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inventory" / "import_graph.py"


@pytest.fixture
def ig(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("import_graph_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app, tests = tmp_path / "backend" / "app", tmp_path / "backend" / "tests"
    for pkg in ("", "core", "services", "tools", "agent_runtime"):
        (app / pkg).mkdir(parents=True, exist_ok=True)
        (app / pkg / "__init__.py").write_text("")
    tests.mkdir()
    files = {
        "main.py": "from app.services import coordinator\n",                          # a seam: registers everything
        "bootstrap.py": "from app.main import create_app\nfrom app.services.verifier import V\n",
        "services/coordinator.py": "def f():\n    from app.tools.executor import run   # function-level import\n",
        "services/verifier.py": "from app.agent_runtime.errors import classify\nV = 1\n",
        "tools/executor.py": "def run(): ...\n",
        "agent_runtime/errors.py": "from app.agent_runtime.graph import G\ndef classify(): ...\n",
        "agent_runtime/graph.py": "G = 1\n",
        "agent_runtime/orphan_demo.py": "X = 1\n",
    }
    for rel, body in files.items():
        (app / rel).write_text(body)
    (tests / "test_kernel.py").write_text("from app.services.verifier import V\n")
    (tests / "test_m1.py").write_text(
        "from app.agent_runtime.graph import G\nfrom app.tools.executor import run\nfrom app.agent_runtime.orphan_demo import X\n")
    (tests / "test_mixed.py").write_text("from app.services.verifier import V\nfrom app.tools.executor import run\n")
    (tests / "test_mixed_kernel.py").write_text(
        "from app.services.verifier import V\nfrom app.main import create_app\nfrom app.tools.executor import run\n")
    (tests / "test_plain.py").write_text("import json\n")
    monkeypatch.setattr(mod, "APP", app)
    monkeypatch.setattr(mod, "TESTS", tests)
    monkeypatch.setattr(mod, "KERNEL_ROOTS", ["app.bootstrap"])
    monkeypatch.setattr(mod, "SEAMS", ["app.main"])
    monkeypatch.setattr(mod, "M1_RE", mod.re.compile(r"app\.(agent_runtime\..*|tools\..*)"))
    return mod


def test_imports_inside_functions_are_found_and_seams_are_not_followed(ig):
    graph, _ = ig.build()
    assert "app.tools.executor" in graph["app.services.coordinator"]                 # function-level import
    kept = ig.reachable(graph, ig.KERNEL_ROOTS, ig.SEAMS)
    assert "app.services.coordinator" not in kept                                     # only app.main imports it (seam)
    assert {"app.services.verifier", "app.agent_runtime.errors", "app.agent_runtime.graph"} <= kept
    assert "app.agent_runtime.orphan_demo" not in kept and "app.tools.executor" not in kept


def test_why_gives_the_chain_that_keeps_a_module_alive(ig):
    graph, _ = ig.build()
    chain = ig.why(graph, "app.agent_runtime.graph", ig.KERNEL_ROOTS, ig.SEAMS)
    assert chain == ["app.bootstrap", "app.services.verifier", "app.agent_runtime.errors", "app.agent_runtime.graph"]
    assert ig.why(graph, "app.agent_runtime.orphan_demo", ig.KERNEL_ROOTS, ig.SEAMS) is None


def test_boundary_and_test_classification(ig):
    r = ig.analyse()
    assert ("app.services.verifier", "app.agent_runtime.errors") in r["boundary"]    # kernel -> M1: a place to refactor
    assert ("app.services.coordinator", "app.tools.executor") in r["boundary"]
    assert "app.agent_runtime.orphan_demo" in r["unreachable_now"]
    classes = {k: set(v) for k, v in r["test_classes"].items()}
    assert classes["only M1: deleted together with it"] == {"test_m1.py"}
    assert classes["mixed, mostly M1 (>=50% of imports): probably deleted, review first"] == {"test_mixed.py"}
    assert classes["mixed, mostly kernel (<50% M1 imports): edit the M1 half out"] == {"test_mixed_kernel.py"}
    assert classes["kernel only: stays"] == {"test_kernel.py"}
    assert classes["no app imports (generic/support)"] == {"test_plain.py"}


def test_imported_only_by_main_is_live_not_dead(ig):
    """The ADR-003 mistake, pinned: app.main registers routers, so a module that ONLY main imports is a live endpoint.
    "Unreachable from the kernel" overstated what is deletable (24 modules claimed, 15 really dead in production)."""
    r = ig.analyse()
    assert "app.services.coordinator" in r["unreachable_now"]                         # looks unused from the kernel...
    assert "app.services.coordinator" not in r["dead_in_prod"]                        # ...but main imports it: live
    assert {"app.services.coordinator", "app.tools.executor"} <= set(r["only_via_main"])
    assert "app.agent_runtime.orphan_demo" in r["dead_in_prod"]
    assert not {"app.services.coordinator", "app.tools.executor", "app.services.verifier"} & set(r["dead_in_prod"])
    # dead in production but still imported by a test: deleting the module would orphan that test
    assert r["held_by_tests"]["app.agent_runtime.orphan_demo"] == ["test_m1.py"]


# ADR-003 ratchet on the REAL repository: kernel -> M1 imports that still exist. The list only shrinks: every cleanup
# step deletes lines here (C2 cut 6 of the original 20; C3 removes the rest). A NEW kernel -> M1 import, or a swapped
# one, fails this test: that is the point (a count would let one edge be traded for another).
REMAINING_BOUNDARY = {
    ("app.main", "app.api.approvals"),
    ("app.main", "app.api.evidence"),
    ("app.main", "app.services.local_approval_access"),
    ("app.services.agent_run_reads", "app.services.approval_contracts"),
    ("app.services.agent_run_reads", "app.services.tool_call_audit"),
    ("app.services.agent_run_reads", "app.services.tool_calls"),
    ("app.services.agent_run_reads", "app.services.verification_evidence"),
    ("app.services.agent_runner", "app.services.approval_outcome"),
    ("app.services.agent_runs", "app.services.approval_outcome"),
    ("app.services.agent_runs", "app.services.tool_call_audit"),
    ("app.services.agent_service", "app.services.approval_outcome"),
    ("app.services.in_process_run_coordinator", "app.schemas.approval"),
    ("app.services.in_process_run_coordinator", "app.services.approval_contracts"),
    ("app.services.in_process_run_coordinator", "app.services.approval_preview"),
}


def test_kernel_to_m1_boundary_never_grows():
    spec = importlib.util.spec_from_file_location("import_graph_real", SCRIPT)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    boundary = set(real.analyse()["boundary"])
    assert boundary <= REMAINING_BOUNDARY, f"new kernel -> M1 imports: {sorted(boundary - REMAINING_BOUNDARY)}"
    assert not (REMAINING_BOUNDARY - boundary), (
        f"cut done, delete from REMAINING_BOUNDARY: {sorted(REMAINING_BOUNDARY - boundary)}")
