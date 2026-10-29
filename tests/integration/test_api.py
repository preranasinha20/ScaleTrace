"""Smoke tests for the web API used by the TypeScript frontend (scaletrace/api/server.py)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from scaletrace.api import server  # noqa: E402

client = TestClient(server.app)
DEMO = "examples/shop_app"


def test_health():
    assert client.get("/api/health").json()["status"] == "ok"


def test_projects_lists_demos_and_never_benchmark_data():
    data = client.get("/api/projects").json()
    paths = [p["path"] for p in data["projects"]]
    assert DEMO in paths and data["default"] == DEMO
    assert not any(p.startswith("benchmark_data") or p.startswith("frontend") for p in paths)
    shop = next(p for p in data["projects"] if p["path"] == DEMO)
    assert shop["demo"] and shop["runtime_report"]


def test_analyze_matches_pipeline():
    data = client.post("/api/analyze", json={"project": DEMO}).json()
    from scaletrace.pipeline import run_static_analysis

    static = run_static_analysis(server.ALLOWED_ROOT / DEMO).report
    assert data["summary"]["findings"] == len(static.findings) == len(data["findings"])
    assert {f["finding_id"] for f in data["findings"]} == {f.finding_id for f in static.findings}
    assert data["runtime"]["used"] is True and data["runtime"]["executes_code_from_api"] is False
    assert data["diagnoses"] and data["diagnoses"][0]["candidates"]
    assert data["ranking"] == "heuristic"


def test_static_only_has_no_runtime_confirmation():
    data = client.post("/api/analyze", json={"project": DEMO, "use_runtime": False}).json()
    assert data["runtime"]["used"] is False
    assert set(data["summary"]["findings_by_status"]) == {"static_candidate"}


def test_reported_endpoint_creates_diagnosis():
    data = client.post("/api/analyze", json={"project": DEMO, "use_runtime": False, "endpoint": "GET /customers/report"}).json()
    assert [d["symptom"]["endpoint"] for d in data["diagnoses"]] == ["GET /customers/report"]
    assert data["diagnoses"][0]["status"] == "static_only"


def test_graph_has_levels_and_relevance():
    data = client.post("/api/graph", json={"project": DEMO}).json()
    assert data["nodes"] and data["edges"]
    assert all("level" in n and "relevant" in n for n in data["nodes"])


@pytest.mark.parametrize("fmt", list(server.EXPORTS))
def test_exports(fmt):
    response = client.post(f"/api/export/{fmt}", json={"project": DEMO})
    assert response.status_code == 200
    assert server.EXPORTS[fmt][0] in response.headers["content-disposition"]
    assert response.text


def test_rejects_unknown_inputs():
    assert client.post("/api/analyze", json={"project": "benchmark_data"}).status_code == 404
    assert client.post("/api/analyze", json={"project": "../"}).status_code == 404
    assert client.post("/api/analyze", json={"project": DEMO, "model": "../evil"}).status_code == 404
    assert client.post("/api/export/exe", json={"project": DEMO}).status_code == 422


def test_source_context_and_traversal():
    ok = client.get("/api/source", params={"project": DEMO, "file": "app/routers/catalog.py", "line": 20}).json()
    assert ok["start"] <= 20 and ok["lines"]
    for bad in ("../../README.md", "../shop_app/scaletrace_workload.json", "/etc/passwd", ".scaletrace/runtime.json"):
        assert client.get("/api/source", params={"project": DEMO, "file": bad, "line": 1}).status_code == 404


def test_frontend_routes_fall_back_to_index():
    if not (server.FRONTEND_DIST / "index.html").is_file():
        pytest.skip("frontend not built (npm --prefix frontend run build)")
    for path in ("/", "/privacy", "/terms"):
        response = client.get(path)
        assert response.status_code == 200 and '<div id="root">' in response.text
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/api/unknown").status_code == 404
