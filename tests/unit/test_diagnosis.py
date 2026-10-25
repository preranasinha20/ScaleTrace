"""Correlation, symptoms and backtracking on hand-built runtime reports (nothing is executed)."""

from __future__ import annotations

import pytest

from scaletrace.diagnosis import DiagnosisConfig, diagnose_repository, user_symptom
from scaletrace.pipeline import run_static_analysis
from scaletrace.runtime.analysis import growth_class, growth_exponent
from scaletrace.schemas.runtime import EndpointRuntime, Measurement, RuntimeReport
from tests.conftest import EXAMPLES

DEFECTIVE = EXAMPLES / "orders_app_defective"
HANDLER = "app.main:list_order_summaries"


def test_growth_exponent():
    assert growth_exponent([10, 100, 1000], [11, 101, 1001], offset=1.0) == pytest.approx(0.97, abs=0.02)
    assert growth_exponent([10, 100, 1000], [1, 1, 1], offset=1.0) == pytest.approx(0.0)
    assert growth_exponent([10, 100], [None, 5]) is None
    assert growth_class(1.0) == "linear" and growth_class(0.0) == "constant" and growth_class(2.0) == "super-linear"


def _runtime(queries: list[float], rss: list[float], tracing: bool = True) -> RuntimeReport:
    sizes = [10, 100, 1000]
    measurements = [
        Measurement(
            size=s, requests=5, errors=0, latency_ms_mean=q * 0.5 + 2, latency_ms_p95=q * 0.6 + 3,
            queries_per_request=q, query_time_ms_per_request=q * 0.4, rss_mb_peak=r,
            function_calls_per_request={HANDLER: 1.0, "app.database:get_db": 1.0} if tracing else {},
            queries_by_function_per_request={HANDLER: q},
        )
        for s, q, r in zip(sizes, queries, rss)
    ]
    return RuntimeReport(
        scaletrace_version="t", repository="orders", app="app.main:app", generated_at="now", status="completed",
        sizes=sizes, instrumentation={"sqlalchemy_queries": True, "call_tracing": tracing, "process_resources": True},
        endpoints=[EndpointRuntime(endpoint="GET /orders/summary", request_name="summary", measurements=measurements)],
    )


def test_growing_queries_correlate_db001_and_not_memory_findings():
    static = run_static_analysis(DEFECTIVE)
    report, _ = diagnose_repository(static, _runtime([11, 101, 1001], [70, 70.5, 71]))
    statuses = {(f.rule_id, f.line): f.status.value for f in report.static_report.findings}
    assert statuses[("DB-001", 56)] == "correlated_finding"
    assert statuses[("MEM-001", 53)] == "not_confirmed"
    [diagnosis] = report.diagnoses
    assert diagnosis.status == "correlated"
    assert diagnosis.symptom.kind == "query_growth"
    assert diagnosis.path_labels == ["GET /orders/summary", "list_order_summaries", "Database"]
    assert diagnosis.related_findings == ["DB-001:app/main.py:56:17"]
    assert "probable cause" in diagnosis.probable_cause.lower()
    top = diagnosis.candidates[0]
    assert top.label == "list_order_summaries" and top.static_score == 1.0 and top.dynamic_score > 0.9


def test_constant_queries_leave_db001_not_confirmed_and_no_diagnosis():
    static = run_static_analysis(DEFECTIVE)
    report, _ = diagnose_repository(static, _runtime([11, 11, 11], [70, 70, 70]))
    statuses = {(f.rule_id, f.line): f.status.value for f in report.static_report.findings}
    assert statuses[("DB-001", 56)] == "not_confirmed"
    assert report.diagnoses == []


def test_static_only_reported_symptom_is_approximate():
    static = run_static_analysis(DEFECTIVE)
    report, _ = diagnose_repository(static, None, reported=[user_symptom(endpoint="GET /orders/summary")])
    [diagnosis] = report.diagnoses
    assert diagnosis.status == "static_only"
    assert diagnosis.approximate is True
    assert all(f.status.value == "static_candidate" for f in report.static_report.findings)
    assert diagnosis.candidates[0].dynamic_score is None and "D" in diagnosis.candidates[0].missing_terms


def test_symptom_without_static_mapping_is_unexplained(tmp_path):
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/x')\ndef x():\n    return compute()\n",
        encoding="utf-8",
    )
    static = run_static_analysis(tmp_path)
    runtime = RuntimeReport(
        scaletrace_version="t", repository="x", app="main:app", generated_at="now", status="completed",
        instrumentation={"sqlalchemy_queries": True, "call_tracing": True},
        endpoints=[EndpointRuntime(endpoint="GET /x", request_name="x", measurements=[
            Measurement(size=s, requests=3, errors=0, latency_ms_p95=lat, latency_ms_mean=lat, queries_per_request=0,
                        function_calls_per_request={"main:x": 1})
            for s, lat in [(10, 5.0), (100, 60.0), (1000, 700.0)]
        ])],
    )
    report, _ = diagnose_repository(static, runtime)
    [diagnosis] = report.diagnoses
    assert diagnosis.status == "unexplained"
    assert diagnosis.probable_cause.startswith("Cause unconfirmed")
    assert len(report.unexplained_observations) == 1


def test_failed_runtime_falls_back_to_static():
    static = run_static_analysis(DEFECTIVE)
    failed = _runtime([11, 101, 1001], [70, 70, 70]).model_copy(update={"status": "failed", "error": "boom"})
    report, _ = diagnose_repository(static, failed)
    assert report.runtime_available is False
    assert any("boom" in item for item in report.limitations)


def test_weights_are_configurable():
    static = run_static_analysis(DEFECTIVE)
    config = DiagnosisConfig(w_static=1.0, w_dynamic=0.0, w_importance=0.0)
    report, _ = diagnose_repository(static, _runtime([11, 101, 1001], [70, 70, 70]), config)
    top = report.diagnoses[0].candidates[0]
    assert top.risk == pytest.approx(top.static_score)
