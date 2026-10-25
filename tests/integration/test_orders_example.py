"""Acceptance test for the first milestone.

Given a small FastAPI application with a known query-inside-loop pattern, ScaleTrace reports
the rule ID, file and line, and produces a JSON finding -- and stays silent on the fixed variant.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scaletrace.evaluation import load_groundtruth, match_findings
from scaletrace.pipeline import analyze_repository
from scaletrace.schemas import AnalysisReport
from tests.conftest import EXAMPLES, REPO_ROOT

DEFECTIVE = EXAMPLES / "orders_app_defective"
FIXED = EXAMPLES / "orders_app_fixed"


@pytest.mark.parametrize("app_dir", [DEFECTIVE, FIXED], ids=["defective", "fixed"])
def test_findings_match_groundtruth(app_dir):
    truth = load_groundtruth(app_dir / "scaletrace_groundtruth.json")
    report = analyze_repository(app_dir)

    result = match_findings(report.findings, truth)
    assert result.false_negatives == []
    assert result.false_positives == []
    assert len(result.true_positives) == len(truth.expected_findings)


def test_defective_finding_details():
    [finding] = analyze_repository(DEFECTIVE, rule_ids=["DB-001"]).findings
    assert (finding.rule_id, finding.file, finding.line) == ("DB-001", "app/main.py", 56)
    assert finding.function == "list_order_summaries"
    assert finding.endpoint == "GET /orders/summary"
    assert finding.status.value == "static_candidate"
    assert "db.query(Order)" in finding.static_evidence[0].snippet


def test_cli_writes_valid_json_report(tmp_path):
    out = tmp_path / "report.json"
    completed = subprocess.run(
        [sys.executable, "-m", "scaletrace", "analyze", str(DEFECTIVE), "--json", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "DB-001" in completed.stdout
    assert "app/main.py:56" in completed.stdout

    data = json.loads(out.read_text(encoding="utf-8"))
    report = AnalysisReport.model_validate(data)  # round-trips through the schema
    assert report.rules_applied == ["DB-001", "DB-003", "CMP-001", "MEM-001", "MEM-002", "EXT-001"]

    [finding] = [f for f in data["findings"] if f["rule_id"] == "DB-001"]
    required = {
        "rule_id", "category", "severity", "classification", "file", "line", "function",
        "endpoint", "message", "static_evidence", "runtime_evidence", "status",
    }
    assert required <= finding.keys()
    assert finding["rule_id"] == "DB-001"
    assert finding["status"] == "static_candidate"


def test_cli_rejects_unknown_rule():
    completed = subprocess.run(
        [sys.executable, "-m", "scaletrace", "analyze", str(DEFECTIVE), "--rules", "XYZ-999"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "XYZ-999" in completed.stderr
