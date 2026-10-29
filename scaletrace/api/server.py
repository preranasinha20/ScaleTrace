"""FastAPI adapter that exposes the existing ScaleTrace pipeline to the TypeScript frontend.

    pip install -e ".[api]"
    python -m scaletrace.api            # http://127.0.0.1:8000

It does the same work as the Streamlit app (``scaletrace/app/streamlit_app.py``) and calls the same
functions; no analysis logic lives here. Safety rules:

- Only folders under SCALETRACE_ALLOWED_ROOT (default: the current working directory) can be analysed,
  and benchmark_data (untrusted third-party code, protected benchmark) is never offered.
- Analysis parses code; it never imports or runs it. The API has no endpoint that executes a workload.
  It can only read a runtime report that was already recorded with the CLI (``.scaletrace/runtime.json``).
- Only GNN checkpoints from ScaleTrace's own v5 experiment can be selected (loaded with weights_only=True).
- ZIP uploads (``/api/uploads``) are extracted by ``scaletrace/api/uploads.py`` into a private temporary
  directory, parsed once, and the directory is deleted before the response is sent. Uploaded code is never
  imported, run or installed, and an uploaded runtime report is never read. Results stay in memory for a
  limited time (see UPLOAD_TTL_SECONDS) and are never written to disk or sent to third parties.

If ``frontend/dist`` exists, it is served at ``/`` with a fallback to ``index.html`` so client routes
such as ``/privacy`` work on refresh.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Literal

import networkx as nx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from scaletrace.api.uploads import UploadError, UploadLimits, extract, read_limited, safe_stem

from scaletrace import __version__
from scaletrace.diagnosis import diagnose_repository, user_symptom
from scaletrace.graph import graph_to_json, risk_paths
from scaletrace.graph.html import _levels, render_graph_html
from scaletrace.graph.knowledge_graph import NodeKind
from scaletrace.pipeline import run_static_analysis
from scaletrace.reports.diagnosis_report import render_diagnosis_html, render_diagnosis_markdown
from scaletrace.runtime import WORKLOAD_FILE

ALLOWED_ROOT = Path(os.environ.get("SCALETRACE_ALLOWED_ROOT", os.getcwd())).resolve()
SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "scaletrace_output", "benchmark_data", "frontend"}
MODEL_DIR = ALLOWED_ROOT / "scaletrace_output" / "ml_v5" / "experiment" / "models"
DEMO_DIR = "examples"
DEFAULT_PROJECT = "examples/shop_app"
RUNTIME_FILE = Path(".scaletrace") / "runtime.json"
FRONTEND_DIST = Path(os.environ.get("SCALETRACE_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
SOURCE_CONTEXT_LINES = 8
CACHE_SIZE = 16

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

UPLOAD_LIMITS = UploadLimits.from_env()
UPLOAD_TMP = os.environ.get("SCALETRACE_UPLOAD_TMP") or None  # None: the system temp directory
UPLOAD_TTL_SECONDS = int(os.environ.get("SCALETRACE_UPLOAD_TTL_SECONDS", 30 * 60))
UPLOAD_MAX_STORED = int(os.environ.get("SCALETRACE_UPLOAD_MAX_STORED", 8))
UPLOAD_CONCURRENCY = int(os.environ.get("SCALETRACE_UPLOAD_CONCURRENCY", 2))
log = logging.getLogger("scaletrace.api")


# --- project discovery (same rules as the Streamlit UI) -------------------------------------------------

def candidate_repositories(root: Path = ALLOWED_ROOT, max_depth: int = 3) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith(".") and depth < max_depth)
        if any(name.endswith(".py") for name in filenames) or "app" in dirnames:
            found.append(current.relative_to(root).as_posix() if current != root else ".")
    return sorted(found)


def demo_projects(repos: list[str]) -> list[str]:
    tops = {"/".join(r.split("/")[:2]) for r in repos if r.startswith(DEMO_DIR + "/")}
    return sorted(t for t in tops if t in repos)


def resolve_project(relative: str) -> Path:
    if relative not in candidate_repositories():
        raise HTTPException(404, f"Unknown project {relative!r}. Choose one from /api/projects.")
    path = (ALLOWED_ROOT / relative).resolve()
    if not (path == ALLOWED_ROOT or ALLOWED_ROOT in path.parents):
        raise HTTPException(400, "Project is outside the allowed root.")
    return path


def available_models() -> list[str]:
    if not MODEL_DIR.is_dir():
        return []
    return sorted(p.stem for p in MODEL_DIR.glob("*.pt") if not p.name.startswith("loko_"))


def _fingerprint(repo: Path) -> tuple:
    """Cheap change detector so cached results are recomputed after code edits."""
    stamps = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        stamps.extend(os.stat(os.path.join(dirpath, f)).st_mtime_ns for f in filenames if f.endswith(".py"))
    runtime = repo / RUNTIME_FILE
    return len(stamps), max(stamps, default=0), runtime.stat().st_mtime_ns if runtime.exists() else 0


# --- analysis ----------------------------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    project: str = DEFAULT_PROJECT
    use_runtime: bool = Field(default=True, description="Use the recorded runtime report if the project has one.")
    endpoint: str = Field(default="", max_length=200, description="Reported slow endpoint, e.g. 'GET /orders'.")
    model: str | None = Field(default=None, description="GNN checkpoint name from /api/models, or null for heuristic.")


class Analysis:
    def __init__(self, request: AnalyzeRequest, repo: Path, static, runtime, report, graph):
        self.request, self.repo, self.static, self.runtime, self.report, self.graph = request, repo, static, runtime, report, graph


_cache: OrderedDict[tuple, Analysis] = OrderedDict()
_cache_lock = Lock()


def analyze(request: AnalyzeRequest) -> Analysis:
    repo = resolve_project(request.project)
    if request.model is not None and request.model not in available_models():
        raise HTTPException(404, f"Unknown model {request.model!r}. Choose one from /api/models.")
    key = (request.project, request.use_runtime, request.endpoint.strip(), request.model, _fingerprint(repo))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    runtime = None
    runtime_file = repo / RUNTIME_FILE
    if request.use_runtime and runtime_file.exists():
        from scaletrace.schemas.runtime import RuntimeReport

        runtime = RuntimeReport.model_validate_json(runtime_file.read_text(encoding="utf-8"))
    result = run_pipeline(request, repo, runtime)
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def run_pipeline(request: AnalyzeRequest, repo: Path | None, runtime, static=None) -> Analysis:
    """The one analysis path shared by workspace projects and uploads: parse, diagnose, optionally rank.

    ``static`` may be a parse kept from an earlier run (uploads re-diagnose after their files are deleted)."""
    static = static if static is not None else run_static_analysis(repo)
    ranker = explainer = None
    if request.model is not None:
        from scaletrace.ml.inference import load_ranker

        ranker, explainer = load_ranker(MODEL_DIR / f"{request.model}.pt", static.project, runtime)
    endpoint = request.endpoint.strip()
    reported = [user_symptom(endpoint=endpoint)] if endpoint else []
    report, graph = diagnose_repository(static, runtime, reported=reported, ranker=ranker,
                                       ranker_name=f"learned ({request.model})" if request.model else "heuristic")
    if explainer is not None:
        for diagnosis in report.diagnoses:
            if diagnosis.candidates:
                diagnosis.explanation = explainer(graph, diagnosis.symptom, diagnosis.candidates[0].node)
    return Analysis(request, repo, static, runtime, report, graph)


def sorted_findings(a: Analysis):
    return sorted(a.report.static_report.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity.value, 9), f.file, f.line))


def graph_payload(a: Analysis) -> dict[str, Any]:
    data = graph_to_json(a.graph)
    levels = _levels(a.graph)
    relevant = set()
    for node, attrs in a.graph.nodes(data=True):
        if attrs["kind"] == NodeKind.ENDPOINT.value:
            relevant.add(node)
            relevant.update(nx.descendants(a.graph, node))
        if attrs.get("findings"):
            relevant.add(node)
    for node in data["nodes"]:
        node["level"] = levels.get(node["id"], 0)
        node["relevant"] = node["id"] in relevant
    return data


def runtime_status(a: Analysis) -> dict[str, Any]:
    if a.repo is None:  # an upload: its files are gone and uploaded code is never run
        return {"recorded_report_available": False, "used": False, "report_status": None,
                "workload_file_present": False, "executes_code_from_api": False, "generated_at": None}
    recorded = (a.repo / RUNTIME_FILE).exists()
    rt = a.runtime
    return {
        "recorded_report_available": recorded,
        "used": rt is not None and rt.status == "completed",
        "report_status": rt.status if rt is not None else None,
        "workload_file_present": (a.repo / WORKLOAD_FILE).exists(),
        "executes_code_from_api": False,
        "generated_at": getattr(rt, "generated_at", None) if rt is not None else None,
    }


def analysis_payload(a: Analysis) -> dict[str, Any]:
    report, static = a.report, a.report.static_report
    findings = sorted_findings(a)
    return {
        "project": a.request.project,
        "repository": report.repository,
        "scaletrace_version": report.scaletrace_version,
        "generated_at": report.generated_at,
        "request": a.request.model_dump(),
        "ranking": a.request.model or "heuristic",
        "runtime": runtime_status(a),
        "summary": {
            "files_analyzed": static.files_analyzed,
            "files_skipped": len(static.files_skipped),
            "findings": len(findings),
            "findings_by_status": report.findings_by_status,
            "findings_by_severity": {s: sum(f.severity.value == s for f in findings) for s in SEVERITY_ORDER},
            "diagnoses": len(report.diagnoses),
            "rules_applied": static.rules_applied,
            "rules_not_implemented": static.rules_not_implemented,
            "rulebook_version": static.rulebook_version,
            "rulebook_status": static.rulebook_status,
        },
        "findings": [f.model_dump(mode="json") for f in findings],
        "risk_paths": [
            {"endpoint": p.endpoint, "nodes": p.nodes, "labels": [a.graph.nodes[n]["label"] for n in p.nodes],
             "rules": p.rules, "max_severity": p.max_severity, "findings": p.findings,
             "resources": [a.graph.nodes[r]["label"] for r in p.resources]}
            for p in risk_paths(a.graph)
        ],
        "diagnoses": [d.model_dump(mode="json") for d in report.diagnoses],
        "unexplained_observations": [o.model_dump(mode="json") for o in report.unexplained_observations],
        "scoring": report.scoring,
        "limitations": report.limitations,
    }


def findings_csv(findings) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["rule", "severity", "status", "file", "line", "function", "endpoint", "message"])
    for f in findings:
        writer.writerow([f.rule_id, f.severity.value, f.status.value, f.file, f.line, f.function, f.endpoint or "", f.message])
    return out.getvalue()


ExportFormat = Literal["report-html", "report-md", "diagnosis-json", "graph-json", "findings-csv", "graph-html"]
EXPORTS: dict[str, tuple[str, str]] = {
    "report-html": ("scaletrace_report.html", "text/html"),
    "report-md": ("scaletrace_report.md", "text/markdown"),
    "diagnosis-json": ("diagnosis.json", "application/json"),
    "graph-json": ("graph.json", "application/json"),
    "findings-csv": ("findings.csv", "text/csv"),
    "graph-html": ("graph.html", "text/html"),
}


def render_export(a: Analysis, fmt: str) -> str:
    if fmt == "report-html":
        return render_diagnosis_html(a.report, a.graph, graph_file=None)
    if fmt == "report-md":
        return render_diagnosis_markdown(a.report, a.graph)
    if fmt == "diagnosis-json":
        return a.report.model_dump_json(indent=2)
    if fmt == "graph-json":
        return json.dumps(graph_to_json(a.graph), indent=2, default=str)
    if fmt == "findings-csv":
        return findings_csv(sorted_findings(a))
    if fmt == "graph-html":
        return render_graph_html(a.graph, a.report.static_report.findings, a.report.repository)
    raise HTTPException(404, f"Unknown export format {fmt!r}")


# --- app ---------------------------------------------------------------------------------------------------

app = FastAPI(title="ScaleTrace API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("SCALETRACE_CORS_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get("/api/projects")
def projects() -> dict[str, Any]:
    repos = candidate_repositories()
    demos = demo_projects(repos)
    items = []
    for rel in repos:
        path = ALLOWED_ROOT / rel
        items.append({"path": rel, "demo": rel in demos, "runtime_report": (path / RUNTIME_FILE).exists(),
                      "workload_file": (path / WORKLOAD_FILE).exists()})
    return {"allowed_root_name": ALLOWED_ROOT.name, "default": DEFAULT_PROJECT if DEFAULT_PROJECT in repos else (demos or repos or [None])[0],
            "projects": items}


@app.get("/api/models")
def models() -> dict[str, Any]:
    return {"models": available_models(),
            "note": "v5 GNN checkpoints. On the real static-only benchmark no learned model beat the rules-only "
                    "baseline (docs/benchmark_results.md). Scores rank candidates; they are not probabilities."}


@app.post("/api/analyze")
def analyze_endpoint(request: AnalyzeRequest) -> dict[str, Any]:
    return analysis_payload(analyze(request))


@app.post("/api/graph")
def graph_endpoint(request: AnalyzeRequest) -> dict[str, Any]:
    return graph_payload(analyze(request))


@app.post("/api/export/{fmt}")
def export_endpoint(fmt: ExportFormat, request: AnalyzeRequest) -> Response:
    a = analyze(request)
    filename, media = EXPORTS[fmt]
    return Response(render_export(a, fmt), media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/source")
def source(project: str = Query(...), file: str = Query(..., max_length=500), line: int = Query(..., ge=1),
           context: int = Query(SOURCE_CONTEXT_LINES, ge=0, le=40)) -> dict[str, Any]:
    """A few lines around a finding, read as text from a .py file inside the selected project."""
    repo = resolve_project(project)
    path = (repo / file).resolve()
    if repo not in path.parents or path.suffix != ".py" or not path.is_file() or any(p in SKIP for p in path.relative_to(repo).parts):
        raise HTTPException(404, "Source file not available.")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = max(1, line - context), min(len(lines), line + context)
    return {"file": file, "start": start, "highlight": line, "lines": lines[start - 1:end]}


# --- ZIP uploads ---------------------------------------------------------------------------------------------

@dataclass
class StoredUpload:
    """What is kept after an upload's files are deleted: the parse, short source excerpts, and results."""

    upload_id: str
    name: str
    info: dict[str, Any]
    static: Any  # StaticAnalysis (in memory; the files themselves are gone)
    sources: dict[tuple[str, int], dict[str, Any]]
    expires_at: float
    analyses: OrderedDict[tuple, Analysis] = field(default_factory=OrderedDict)


class UploadStore:
    def __init__(self) -> None:
        self._items: OrderedDict[str, StoredUpload] = OrderedDict()
        self._lock = Lock()

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            del self._items[key]

    def put(self, item: StoredUpload) -> None:
        with self._lock:
            self._purge()
            self._items[item.upload_id] = item
            while len(self._items) > UPLOAD_MAX_STORED:
                self._items.popitem(last=False)

    def get(self, upload_id: str) -> StoredUpload:
        with self._lock:
            self._purge()
            item = self._items.get(upload_id)
        if item is None:
            raise HTTPException(404, "This upload is not available (it expired, was discarded, or the server restarted). Upload the ZIP again.")
        return item

    def delete(self, upload_id: str) -> bool:
        with self._lock:
            return self._items.pop(upload_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)


uploads = UploadStore()
_upload_slots = BoundedSemaphore(max(1, UPLOAD_CONCURRENCY))


def _upload_analysis(item: StoredUpload, endpoint: str, model: str | None) -> Analysis:
    if model is not None and model not in available_models():
        raise HTTPException(404, f"Unknown model {model!r}. Choose one from /api/models.")
    key = (endpoint.strip(), model)
    if key in item.analyses:
        return item.analyses[key]
    request = AnalyzeRequest(project=f"upload:{item.name}", use_runtime=False, endpoint=endpoint, model=model)
    analysis = run_pipeline(request, None, None, static=item.static)
    for report in (analysis.report, analysis.report.static_report):
        report.repository = item.name
    item.analyses[key] = analysis
    while len(item.analyses) > 4:
        item.analyses.popitem(last=False)
    return analysis


def _upload_payload(item: StoredUpload, analysis: Analysis) -> dict[str, Any]:
    payload = analysis_payload(analysis)
    payload["upload"] = {**item.info, "upload_id": item.upload_id,
                         "expires_in_seconds": max(0, int(item.expires_at - time.time()))}
    return payload


def process_upload(data: bytes, filename: str, endpoint: str, model: str | None) -> dict[str, Any]:
    """Extract into a private temporary directory, parse once, keep the results, delete the directory."""
    workdir = Path(tempfile.mkdtemp(prefix="scaletrace-upload-", dir=UPLOAD_TMP))
    try:
        destination = workdir / "project"
        destination.mkdir()
        extraction = extract(data, destination, UPLOAD_LIMITS)
        name = extraction.root.name if extraction.project_root else safe_stem(filename)
        try:
            static = run_static_analysis(extraction.root)
            sources: dict[tuple[str, int], dict[str, Any]] = {}
            for finding in static.report.findings:
                path = (extraction.root / finding.file).resolve()
                if extraction.root.resolve() in path.parents and path.is_file():
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    start = max(1, finding.line - SOURCE_CONTEXT_LINES)
                    end = min(len(lines), finding.line + SOURCE_CONTEXT_LINES)
                    sources[(finding.file, finding.line)] = {"file": finding.file, "start": start,
                                                             "highlight": finding.line, "lines": lines[start - 1:end]}
        except Exception:  # never echo internals (paths, tracebacks) to the client
            reference = secrets.token_hex(4)
            log.exception("upload analysis failed (reference %s)", reference)
            raise UploadError(f"ScaleTrace could not analyse this archive (reference {reference}).", 500, "analysis_failed") from None
        info = {"filename": Path(filename).name[:200], "name": name, "project_root": extraction.project_root,
                "python_files": extraction.python_files, "files_extracted": extraction.files_written,
                "bytes_extracted": extraction.bytes_written, "members_skipped": extraction.members_skipped,
                "static_only": True}
        item = StoredUpload(upload_id=secrets.token_urlsafe(16), name=name, info=info, static=static,
                            sources=sources, expires_at=time.time() + UPLOAD_TTL_SECONDS)
        analysis = _upload_analysis(item, endpoint, model)
        payload = _upload_payload(item, analysis)
        uploads.put(item)
        return payload
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            log.error("could not remove an upload's temporary directory")


@app.exception_handler(UploadError)
async def _upload_error(_request: Request, error: UploadError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"detail": error.message, "code": error.code})


@app.get("/api/uploads/config")
def upload_config() -> dict[str, Any]:
    limits = UPLOAD_LIMITS
    return {"max_upload_bytes": limits.max_upload_bytes, "max_extracted_bytes": limits.max_extracted_bytes,
            "max_file_bytes": limits.max_file_bytes, "max_members": limits.max_members, "max_depth": limits.max_depth,
            "ttl_seconds": UPLOAD_TTL_SECONDS}


@app.post("/api/uploads")
async def upload_endpoint(request: Request, filename: str = Query("upload.zip", max_length=200),
                          endpoint: str = Query("", max_length=200), model: str | None = Query(None)) -> dict[str, Any]:
    """Body: the raw ZIP bytes (Content-Type application/zip). Static analysis only; nothing is executed."""
    if model is not None and model not in available_models():
        raise HTTPException(404, f"Unknown model {model!r}. Choose one from /api/models.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > UPLOAD_LIMITS.max_upload_bytes:
        raise UploadError(f"The upload is larger than the {UPLOAD_LIMITS.max_upload_bytes // (1024 * 1024)} MB limit.", 413, "upload_too_large")
    data = await read_limited(request.stream(), UPLOAD_LIMITS.max_upload_bytes)
    if not _upload_slots.acquire(blocking=False):
        raise UploadError("The server is busy analysing other uploads. Try again in a moment.", 429, "busy")
    try:
        return await run_in_threadpool(process_upload, data, filename, endpoint, model)
    finally:
        _upload_slots.release()


@app.post("/api/uploads/{upload_id}/analyze")
def upload_reanalyze(upload_id: str, endpoint: str = Query("", max_length=200), model: str | None = Query(None)) -> dict[str, Any]:
    item = uploads.get(upload_id)
    return _upload_payload(item, _upload_analysis(item, endpoint, model))


@app.get("/api/uploads/{upload_id}/graph")
def upload_graph(upload_id: str, endpoint: str = Query("", max_length=200), model: str | None = Query(None)) -> dict[str, Any]:
    item = uploads.get(upload_id)
    return graph_payload(_upload_analysis(item, endpoint, model))


@app.get("/api/uploads/{upload_id}/export/{fmt}")
def upload_export(upload_id: str, fmt: ExportFormat, endpoint: str = Query("", max_length=200),
                  model: str | None = Query(None)) -> Response:
    item = uploads.get(upload_id)
    filename, media = EXPORTS[fmt]
    return Response(render_export(_upload_analysis(item, endpoint, model), fmt), media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/uploads/{upload_id}/source")
def upload_source(upload_id: str, file: str = Query(..., max_length=500), line: int = Query(..., ge=1)) -> dict[str, Any]:
    """Only the excerpts captured around findings at analysis time; the uploaded files no longer exist."""
    item = uploads.get(upload_id)
    excerpt = item.sources.get((file, line))
    if excerpt is None:
        raise HTTPException(404, "Source excerpt not available for this location.")
    return excerpt


@app.delete("/api/uploads/{upload_id}")
def upload_delete(upload_id: str) -> dict[str, Any]:
    return {"deleted": uploads.delete(upload_id)}


if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
    _dist = FRONTEND_DIST.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (_dist / path).resolve()
        if path and _dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
