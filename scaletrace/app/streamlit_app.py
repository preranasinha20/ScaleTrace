"""ScaleTrace demo UI (Streamlit).

    pip install -e ".[ui]"
    streamlit run scaletrace/app/streamlit_app.py --server.address 127.0.0.1

Only repositories under SCALETRACE_ALLOWED_ROOT (default: the current working directory) can be
selected, so the UI never exposes arbitrary host paths. Static analysis only parses code. Runtime
testing executes target code and needs an explicit confirmation checkbox and a workload file.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from scaletrace.diagnosis import diagnose_repository, user_symptom
from scaletrace.graph import graph_to_json, risk_paths
from scaletrace.graph.html import render_graph_html
from scaletrace.pipeline import run_static_analysis
from scaletrace.reports.diagnosis_report import render_diagnosis_html, render_diagnosis_markdown
from scaletrace.runtime import WORKLOAD_FILE

ALLOWED_ROOT = Path(os.environ.get("SCALETRACE_ALLOWED_ROOT", os.getcwd())).resolve()
# benchmark_data holds untrusted third-party repositories and the protected benchmark: never offered here.
SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "scaletrace_output", "benchmark_data"}
# Only checkpoints ScaleTrace trained itself, from the current (v5) experiment; leave-one-kind-out runs are diagnostics.
MODEL_DIR = ALLOWED_ROOT / "scaletrace_output" / "ml_v5" / "experiment" / "models"
DEMO_DIR = "examples"
STATUS_TEXT = {
    "static_candidate": "Static candidate",
    "runtime_observed": "Seen at runtime",
    "correlated_finding": "Static + runtime agree",
    "not_confirmed": "Not confirmed at runtime",
}
DIAGNOSIS_TEXT = {"correlated": "Static and runtime evidence agree", "static_only": "Static evidence only",
                  "unexplained": "No code-level cause found"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

CSS = """
<style>
.st-badge {display:inline-block; padding:1px 8px; border-radius:999px; font-size:0.78rem; font-weight:600;
           border:1px solid rgba(128,128,128,0.35); margin-right:6px;}
.st-high {background:rgba(220,53,69,0.16); color:#c0392b;}
.st-medium {background:rgba(255,159,28,0.18); color:#b9770e;}
.st-low {background:rgba(40,167,69,0.16); color:#1e8449;}
.st-path {font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.86rem;}
.st-muted {opacity:0.75; font-size:0.85rem;}
</style>
"""


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


def demo_projects(repos: list[str]) -> list[str]:
    """Top-level folders under examples/ that contain code."""
    tops = {"/".join(r.split("/")[:2]) for r in repos if r.startswith(DEMO_DIR + "/")}
    return sorted(t for t in tops if t in repos)


def resolve_inside(relative: str) -> Path | None:
    path = (ALLOWED_ROOT / relative).resolve()
    return path if path == ALLOWED_ROOT or ALLOWED_ROOT in path.parents else None


def badge(severity: str) -> str:
    return f"<span class='st-badge st-{severity}'>{severity.upper()}</span>"


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def findings_csv(findings) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["rule", "severity", "status", "file", "line", "function", "endpoint", "message"])
    for f in findings:
        writer.writerow([f.rule_id, f.severity.value, f.status.value, f.file, f.line, f.function, f.endpoint or "", f.message])
    return out.getvalue()


st.set_page_config(page_title="ScaleTrace", page_icon=":mag:", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)

repos = candidate_repositories(str(ALLOWED_ROOT))
demos = demo_projects(repos)

with st.sidebar:
    st.header("1. Project")
    scope = st.radio("Show", ["Demo projects", "All folders in this workspace"], horizontal=True, label_visibility="collapsed")
    options = demos if scope == "Demo projects" and demos else repos
    default = "examples/shop_app"
    choice = st.selectbox("Repository", options, index=options.index(default) if default in options else 0, key="repo")

    repo = resolve_inside(choice) if choice else None
    if repo is None:
        st.error("Choose a repository inside the allowed root.")
        st.stop()

    st.header("2. Runtime evidence")
    runtime_file = repo / ".scaletrace" / "runtime.json"
    use_cached = runtime_file.exists() and st.checkbox("Use cached runtime report", value=True, help=str(runtime_file))
    if not runtime_file.exists():
        st.caption("No cached runtime report for this project: the diagnosis uses static evidence only.")
    has_workload = (repo / WORKLOAD_FILE).exists()
    consent = st.checkbox(
        "I understand that a runtime test EXECUTES this repository's code locally",
        value=False, disabled=not has_workload,
        help=None if has_workload else f"No {WORKLOAD_FILE} in this repository.",
    )
    run_now = st.button("Run workload now", disabled=not consent)

    st.header("3. Ranking")
    endpoint = st.text_input("Reported slow endpoint (optional)", placeholder="GET /orders/summary")
    models = sorted(p.relative_to(ALLOWED_ROOT).as_posix() for p in MODEL_DIR.glob("*.pt")
                    if not p.name.startswith("loko_")) if MODEL_DIR.is_dir() else []
    model_choice = st.selectbox("Learned ranker (GNN checkpoint)", ["none"] + models, key="model",
                                format_func=lambda m: "none (heuristic ranking)" if m == "none" else Path(m).stem)
    st.caption("The v5 GNN passed its validity checks on synthetic held-out data only. On the real static-only "
               "benchmark it did not beat the rules-only baseline (see docs/benchmark_results.md). Its scores rank "
               "candidates; they are not probabilities.")

st.title("ScaleTrace")
st.markdown("Finds code in a FastAPI backend that will get slower or use more memory as data grows, links it to the "
            "endpoints it affects in a knowledge graph, and ranks the most likely root cause of a slow endpoint.")

with st.spinner("Analysing the code (parsing only, nothing is executed)..."):
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

findings = sorted(report.static_report.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity.value, 9), f.file, f.line))
evidence = "static + cached runtime" if runtime is not None else "static only"
ranking = Path(model_choice).stem if model_choice != "none" else "heuristic"
st.markdown(f"<span class='st-muted'>Project <b>{choice}</b> · evidence: <b>{evidence}</b> · ranking: <b>{ranking}</b></span>",
            unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Python files", static.report.files_analyzed)
col2.metric("Findings", len(findings), help="Code patterns matching a rulebook rule (candidates, not proven defects).")
col3.metric("Runtime-confirmed", report.findings_by_status.get("correlated_finding", 0),
            help="Findings whose runtime measurements grow with input size as the rule predicts.")
col4.metric("Diagnoses", len(report.diagnoses), help="Slow or growing endpoints traced back to candidate root causes.")

tab_overview, tab_graph, tab_diag, tab_findings, tab_export = st.tabs(
    ["Overview", "Knowledge graph", "Root causes", "Findings", "Export"])

with tab_overview:
    paths = risk_paths(graph)
    if not paths:
        st.success("No risk paths: no endpoint reaches code that matches a scalability rule.")
    st.subheader("Risk paths (endpoint → code → resource)")
    for path in paths:
        st.markdown(f"{badge(path.max_severity)}<span class='st-path'>{path.describe(graph)}</span>", unsafe_allow_html=True)
    if report.diagnoses:
        st.subheader("Most likely root causes")
        for diagnosis in report.diagnoses:
            top = diagnosis.candidates[0].label if diagnosis.candidates else "none"
            st.markdown(f"- **{diagnosis.symptom.endpoint or diagnosis.symptom.description}** → `{top}` "
                        f"<span class='st-muted'>({DIAGNOSIS_TEXT.get(diagnosis.status, diagnosis.status)})</span>",
                        unsafe_allow_html=True)

with tab_graph:
    st.caption("Endpoints on the left, functions in the middle, resources (database, external services) on the right. "
               "Red nodes have findings; thick edges were observed at runtime. Click a node for details.")
    components.html(render_graph_html(graph, report.static_report.findings, report.repository), height=720, scrolling=True)

with tab_diag:
    if not report.diagnoses:
        st.info("No symptoms to diagnose. Use a cached runtime report, or enter a reported slow endpoint in the sidebar.")
    for diagnosis in report.diagnoses:
        title = f"{diagnosis.symptom.endpoint or 'Symptom'}: {DIAGNOSIS_TEXT.get(diagnosis.status, diagnosis.status)}"
        with st.expander(title, expanded=True):
            st.markdown(f"**Symptom:** {diagnosis.symptom.description}")
            st.markdown(f"**Path:** <span class='st-path'>{' → '.join(diagnosis.path_labels)}</span>", unsafe_allow_html=True)
            st.markdown(f"**{diagnosis.probable_cause}**")
            st.markdown("**Ranked candidates** (risk combines static, runtime and graph-importance scores)")
            st.dataframe([
                {"rank": i, "function": c.label, "risk": round(c.risk, 3), "static": fmt(c.static_score),
                 "runtime": fmt(c.dynamic_score), "importance": fmt(c.importance),
                 "gnn": fmt(c.gnn_score), "rules": ", ".join(c.rules) or "-",
                 "location": f"{c.file}:{c.line}" if c.file else ""}
                for i, c in enumerate(diagnosis.candidates, start=1)
            ], hide_index=True, width="stretch")
            with st.popover("How was this reached?") if hasattr(st, "popover") else st.container():
                for step in diagnosis.steps:
                    st.markdown(f"{step.order}. *({step.evidence})* {step.text}")
            if diagnosis.explanation:
                with st.popover("GNN explanation") if hasattr(st, "popover") else st.container():
                    st.json(diagnosis.explanation, expanded=False)

with tab_findings:
    if not findings:
        st.success("No findings in this project.")
    else:
        c1, c2, c3 = st.columns(3)
        rules = c1.multiselect("Rule", sorted({f.rule_id for f in findings}))
        severities = c2.multiselect("Severity", [s for s in SEVERITY_ORDER if any(f.severity.value == s for f in findings)])
        statuses = c3.multiselect("Status", sorted({f.status.value for f in findings}), format_func=lambda s: STATUS_TEXT.get(s, s))
        shown = [f for f in findings if (not rules or f.rule_id in rules) and (not severities or f.severity.value in severities)
                 and (not statuses or f.status.value in statuses)]
        st.dataframe([
            {"rule": f.rule_id, "severity": f.severity.value, "status": STATUS_TEXT.get(f.status.value, f.status.value),
             "location": f"{f.file}:{f.line}", "function": f.function, "endpoint": f.endpoint or ""}
            for f in shown
        ], hide_index=True, width="stretch")
        if shown:
            pick = st.selectbox("Finding details", range(len(shown)),
                                format_func=lambda i: f"{shown[i].rule_id} · {shown[i].file}:{shown[i].line} · {shown[i].function}")
            f = shown[pick]
            st.markdown(f"{badge(f.severity.value)} **{f.rule_id}: {f.rule_name}**", unsafe_allow_html=True)
            st.markdown(f.message)
            for ev in f.static_evidence:
                st.markdown(ev.description)
                st.code(ev.snippet, language="python")
            for ev in f.runtime_evidence:
                st.markdown(f"*Runtime:* {ev.description}")
            if f.possible_false_positives:
                st.markdown("**Could be a false positive if:** " + "; ".join(f.possible_false_positives))

with tab_export:
    st.markdown("Download the results of this analysis.")
    e1, e2, e3 = st.columns(3)
    e1.download_button("Report (HTML)", render_diagnosis_html(report, graph, graph_file=None), "scaletrace_report.html", "text/html")
    e1.download_button("Report (Markdown)", render_diagnosis_markdown(report, graph), "scaletrace_report.md", "text/markdown")
    e2.download_button("Diagnosis (JSON)", report.model_dump_json(indent=2), "diagnosis.json", "application/json")
    e2.download_button("Knowledge graph (JSON)", json.dumps(graph_to_json(graph), indent=2), "graph.json", "application/json")
    e3.download_button("Findings (CSV)", findings_csv(findings), "findings.csv", "text/csv")
    e3.download_button("Knowledge graph (HTML)", render_graph_html(graph, report.static_report.findings, report.repository),
                       "graph.html", "text/html")
