"""ScaleTrace prototype UI (Streamlit).

    pip install -e ".[ui]"
    streamlit run scaletrace/app/streamlit_app.py

Only repositories under SCALETRACE_ALLOWED_ROOT (default: the current working directory) can be
selected, so the UI never exposes arbitrary host paths. Runtime testing executes target code and
needs an explicit confirmation checkbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from scaletrace.diagnosis import diagnose_repository, user_symptom
from scaletrace.graph import graph_to_json, risk_paths
from scaletrace.graph.html import render_graph_html
from scaletrace.pipeline import run_static_analysis
from scaletrace.reports.diagnosis_report import render_diagnosis_markdown
from scaletrace.runtime import WORKLOAD_FILE

ALLOWED_ROOT = Path(os.environ.get("SCALETRACE_ALLOWED_ROOT", os.getcwd())).resolve()
SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "scaletrace_output"}


@st.cache_data(show_spinner=False)
def candidate_repositories(root: str, max_depth: int = 3) -> list[str]:
    found = []
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        depth = len(current.relative_to(base).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith(".") and depth < max_depth)
        if any(name.endswith(".py") for name in filenames) or "app" in dirnames:
            found.append(current.relative_to(base).as_posix() if current != base else ".")
    return sorted(found)


def resolve_inside(relative: str) -> Path | None:
    path = (ALLOWED_ROOT / relative).resolve()
    return path if path == ALLOWED_ROOT or ALLOWED_ROOT in path.parents else None


st.set_page_config(page_title="ScaleTrace", layout="wide")
st.title("ScaleTrace")
st.caption(f"Scalability-risk diagnosis for FastAPI backends. Repositories are limited to: {ALLOWED_ROOT}")

repos = candidate_repositories(str(ALLOWED_ROOT))
choice = st.selectbox("Repository", repos, index=repos.index("examples/shop_app") if "examples/shop_app" in repos else 0)
repo = resolve_inside(choice) if choice else None
if repo is None:
    st.error("Choose a repository inside the allowed root.")
    st.stop()

with st.sidebar:
    st.header("Evidence")
    runtime_file = repo / ".scaletrace" / "runtime.json"
    use_cached = runtime_file.exists() and st.checkbox("Use cached runtime report", value=True, help=str(runtime_file))
    has_workload = (repo / WORKLOAD_FILE).exists()
    consent = st.checkbox(
        "I understand that a runtime test EXECUTES this repository's code locally",
        value=False, disabled=not has_workload,
        help=None if has_workload else f"No {WORKLOAD_FILE} in this repository.",
    )
    run_now = st.button("Run workload now", disabled=not consent)
    st.header("Reported symptom (optional)")
    endpoint = st.text_input("Endpoint, e.g. GET /orders/summary")
    models = sorted(str(p.relative_to(ALLOWED_ROOT)) for p in ALLOWED_ROOT.rglob("*.pt") if ".venv" not in p.parts)
    model_choice = st.selectbox("Learned ranker (GNN checkpoint)", ["none"] + models)

static = run_static_analysis(repo)
runtime = None
if run_now:
    from scaletrace.ml.cli import _collect_one

    with st.spinner("Running workload (executing target code)..."):
        st.info(_collect_one(repo, force=True))
    use_cached = True
if use_cached and runtime_file.exists():
    from scaletrace.schemas.runtime import RuntimeReport

    runtime = RuntimeReport.model_validate_json(runtime_file.read_text(encoding="utf-8"))

ranker = explainer = None
if model_choice != "none":
    from scaletrace.ml.inference import load_ranker

    ranker, explainer = load_ranker(ALLOWED_ROOT / model_choice, static.project, runtime)
reported = [user_symptom(endpoint=endpoint.strip())] if endpoint.strip() else []
report, graph = diagnose_repository(static, runtime, reported=reported, ranker=ranker, ranker_name="gnn (learned)")
if explainer is not None:
    for diagnosis in report.diagnoses:
        if diagnosis.candidates:
            diagnosis.explanation = explainer(graph, diagnosis.symptom, diagnosis.candidates[0].node)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Python files", static.report.files_analyzed)
col2.metric("Findings", len(report.static_report.findings))
col3.metric("Correlated", report.findings_by_status.get("correlated_finding", 0))
col4.metric("Diagnoses", len(report.diagnoses))

tab_graph, tab_diag, tab_findings, tab_export = st.tabs(["Knowledge graph", "Diagnoses", "Findings", "Export"])
with tab_graph:
    for path in risk_paths(graph):
        st.markdown(f"- **{path.max_severity.upper()}** `{path.describe(graph)}`")
    components.html(render_graph_html(graph, report.static_report.findings, report.repository), height=720, scrolling=True)
with tab_diag:
    if not report.diagnoses:
        st.info("No symptoms. Use a runtime report or enter a reported endpoint in the sidebar.")
    for diagnosis in report.diagnoses:
        with st.expander(f"[{diagnosis.status}] {diagnosis.symptom.description}", expanded=True):
            st.markdown(f"**Path:** `{' -> '.join(diagnosis.path_labels)}`")
            for step in diagnosis.steps:
                st.markdown(f"{step.order}. *({step.evidence})* {step.text}")
            st.markdown(f"**{diagnosis.probable_cause}**")
            st.dataframe([c.model_dump(include={'label', 'static_score', 'dynamic_score', 'importance', 'risk', 'gnn_score', 'rules'}) for c in diagnosis.candidates])
            if diagnosis.explanation:
                st.json(diagnosis.explanation, expanded=False)
with tab_findings:
    st.dataframe([
        {"rule": f.rule_id, "severity": f.severity.value, "status": f.status.value, "location": f"{f.file}:{f.line}",
         "function": f.function, "endpoint": f.endpoint, "evidence": f.static_evidence[0].description if f.static_evidence else ""}
        for f in report.static_report.findings
    ], width="stretch")
with tab_export:
    st.download_button("diagnosis.json", report.model_dump_json(indent=2), "diagnosis.json", "application/json")
    st.download_button("graph.json", json.dumps(graph_to_json(graph), indent=2), "graph.json", "application/json")
    st.download_button("report.md", render_diagnosis_markdown(report, graph), "report.md", "text/markdown")
