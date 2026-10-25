"""End-to-end: CLI graph/diagnose/evaluate (static), and a real runtime run of the demo app.

The runtime test EXECUTES examples/orders_app_defective (our own demo code) on 127.0.0.1.
Skip it with: pytest -m "not runtime".
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scaletrace.diagnosis import diagnose_repository
from scaletrace.pipeline import run_static_analysis
from scaletrace.runtime import load_workload
from tests.conftest import EXAMPLES, REPO_ROOT

DEFECTIVE = EXAMPLES / "orders_app_defective"
SHOP = EXAMPLES / "shop_app"


def _cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "scaletrace", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def test_graph_command_writes_json_and_html(tmp_path):
    done = _cli("graph", str(SHOP), "--out", str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "GET /customers/report -> customer_report" in done.stdout
    data = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    assert any(n["id"] == "resource:database" for n in data["nodes"])
    assert (tmp_path / "graph.html").read_text(encoding="utf-8").startswith("<!doctype html>")


def test_diagnose_static_with_reported_symptom(tmp_path):
    done = _cli("diagnose", str(SHOP), "--endpoint", "GET /customers/report", "--out", str(tmp_path))
    assert done.returncode == 0, done.stderr
    report = json.loads((tmp_path / "diagnosis.json").read_text(encoding="utf-8"))
    [diagnosis] = report["diagnoses"]
    assert diagnosis["status"] == "static_only" and diagnosis["approximate"] is True
    assert diagnosis["candidates"][0]["label"] == "customer_report"
    for name in ("report.md", "report.html", "graph.html", "analysis.json"):
        assert (tmp_path / name).exists()


def test_run_requires_explicit_consent():
    done = _cli("run", str(DEFECTIVE))
    assert done.returncode == 2
    assert "--allow-execute" in done.stderr


def test_evaluate_command_scores_examples(tmp_path):
    csv_path = tmp_path / "metrics.csv"
    done = _cli("evaluate", str(DEFECTIVE), str(EXAMPLES / "orders_app_fixed"), str(SHOP), "--csv", str(csv_path))
    assert done.returncode == 0, done.stderr
    assert "overall   all" in done.stdout
    assert "    FP " not in done.stdout and "    FN " not in done.stdout
    assert csv_path.read_text(encoding="utf-8").startswith("level,name,tp,fp,fn")


@pytest.mark.runtime
def test_runtime_run_confirms_db001(tmp_path):
    from scaletrace.runtime.runner import run_workload

    for stale in DEFECTIVE.glob("*.db"):
        stale.unlink()
    spec = load_workload(DEFECTIVE / "scaletrace_workload.json").model_copy(update={"sizes": [10, 100, 400], "repetitions": 2})
    runtime = run_workload(DEFECTIVE, spec, tmp_path)
    assert runtime.status == "completed", runtime.error
    summary = next(e for e in runtime.endpoints if e.endpoint == "GET /orders/summary")
    assert [m.queries_per_request for m in summary.by_size()] == [11, 101, 401]
    assert summary.by_size()[-1].queries_by_function_per_request == {"app.main:list_order_summaries": 401}

    report, graph = diagnose_repository(run_static_analysis(DEFECTIVE), runtime)
    [diagnosis] = [d for d in report.diagnoses if d.symptom.kind == "query_growth"]
    assert diagnosis.status == "correlated" and diagnosis.approximate is False
    assert graph.edges["endpoint:GET /orders/summary", "function:app.main:list_order_summaries", "routes_to"]["observed"]
