"""End-to-end smoke test of the Streamlit demo, driven headlessly (no browser, no target code executed)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "scaletrace" / "app" / "streamlit_app.py"
GNN = "scaletrace_output/ml_v5/experiment/models/gnn_combined_s0.pt"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setitem(os.environ, "SCALETRACE_ALLOWED_ROOT", str(ROOT))
    return AppTest.from_file(str(APP), default_timeout=300).run()


def metrics(at) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def test_demo_flow_static_runtime_and_reports(app):
    assert not app.exception
    repo, model = app.selectbox(key="repo"), app.selectbox(key="model")
    assert repo.value == "examples/shop_app"
    assert not any("benchmark_data" in option for option in repo.options)  # untrusted repos are never offered
    assert model.options[0].startswith("none") and not any(o.startswith("loko_") for o in model.options)
    assert metrics(app) == {"Python files": "9", "Findings": "6", "Runtime-confirmed": "2", "Diagnoses": "2"}
    assert len(app.tabs) == 5
    assert repo.options == ["examples/orders_app_defective", "examples/orders_app_fixed", "examples/shop_app"]
    assert len(app.get("download_button")) == 6  # HTML/Markdown report, diagnosis/graph JSON, findings CSV, graph HTML
    assert app.code  # the selected finding shows its source snippet

    repo.select("examples/orders_app_fixed").run()
    assert not app.exception and metrics(app)["Diagnoses"] == "0"


@pytest.mark.skipif(not (ROOT / GNN).exists(), reason="v5 checkpoint not trained in this checkout")
def test_demo_flow_with_gnn_ranker(app):
    model = app.selectbox(key="model")
    model.select_index(model.options.index("gnn_combined_s0")).run()
    assert not app.exception and metrics(app)["Diagnoses"] == "2"
