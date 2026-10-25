"""Loading labelled applications and turning them into GNN samples.

An application directory is usable when it contains labels -- a benchmark manifest
(``scaletrace_benchmark.json``; only fully verified defects become labels) or the generator's
``scaletrace_rootcause.json`` -- and optionally ``.scaletrace/runtime.json`` (written by
``scaletrace ml collect-runtime``). The ``.scaletrace`` folder is hidden, so static analysis never
treats it as source.

Every loader that feeds training or evaluation passes an ``access`` level to the benchmark guard
(``scaletrace.benchmark.guard``): training never sees benchmark applications, and only the final,
logged evaluation may read the locked test split.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from scaletrace.benchmark.guard import Access, check_access
from scaletrace.benchmark.loader import has_manifest, load_manifest, ml_labels_from_manifest
from scaletrace.diagnosis import DiagnosisConfig, correlate_findings
from scaletrace.graph.knowledge_graph import build_knowledge_graph, merge_runtime
from scaletrace.ml.features import FeatureBuilder, GraphTensors
from scaletrace.ml.generator import ROOTCAUSE_FILE
from scaletrace.pipeline import StaticAnalysis, run_static_analysis
from scaletrace.schemas import Finding
from scaletrace.schemas.runtime import RuntimeReport

RUNTIME_CACHE = Path(".scaletrace") / "runtime.json"


@dataclass
class AppBundle:
    name: str
    path: Path
    static: StaticAnalysis
    runtime: RuntimeReport | None
    labels: dict
    static_graph: nx.MultiDiGraph
    combined_graph: nx.MultiDiGraph | None
    correlated_findings: list[Finding] | None


@dataclass
class Sample:
    app: str
    endpoint: str
    symptom_kind: str
    defect: str
    tensors: GraphTensors

    @property
    def has_positive(self) -> bool:
        return any(self.tensors.labels[i] > 0 for i in self.tensors.candidates)


def _is_labelled(path: Path) -> bool:
    return (path / ROOTCAUSE_FILE).exists() or has_manifest(path)


def find_apps(roots: list[str | Path], access: Access | None = None) -> list[Path]:
    """Labelled applications under ``roots``. With ``access`` set, the benchmark guard is applied
    to every application found (raising on any forbidden one)."""
    apps: list[Path] = []
    for root in roots:
        root = Path(root)
        if _is_labelled(root):
            apps.append(root)
        else:
            apps.extend(sorted(p for p in root.iterdir() if p.is_dir() and _is_labelled(p)))
    if access is not None:
        check_access(apps, access)
    return apps


def load_labels(app_dir: Path, level: str = "runtime") -> dict:
    """Manifest labels (defects verified at ``level``) when a manifest exists, else the generator's file."""
    if has_manifest(app_dir):
        return ml_labels_from_manifest(load_manifest(app_dir), level)
    return json.loads((app_dir / ROOTCAUSE_FILE).read_text(encoding="utf-8"))


def runtime_verified_symptoms(labels: dict) -> dict:
    """Keep only symptoms whose runtime verification passed (generator labels carry it after
    `scaletrace ml verify-labels`); labels without verification info are returned unchanged."""
    symptoms = labels.get("symptoms", [])
    if not any("verification" in s for s in symptoms):
        return labels
    kept = [s for s in symptoms if s.get("verification", {}).get("runtime") == "verified"]
    return {**labels, "symptoms": kept, "rejected_symptoms": len(symptoms) - len(kept)}


def load_bundle(app_dir: str | Path, config: DiagnosisConfig | None = None, require_verified: bool = False,
                label_level: str = "runtime") -> AppBundle:
    app_dir = Path(app_dir)
    config = config or DiagnosisConfig()
    static = run_static_analysis(app_dir)
    labels = load_labels(app_dir, label_level)
    if require_verified:
        labels = runtime_verified_symptoms(labels)
    runtime_file = app_dir / RUNTIME_CACHE
    runtime = None
    if runtime_file.exists():
        candidate = RuntimeReport.model_validate_json(runtime_file.read_text(encoding="utf-8"))
        runtime = candidate if candidate.status == "completed" else None

    static_graph = build_knowledge_graph(static.project, static.report.findings)
    combined_graph = correlated = None
    if runtime is not None:
        func_ids = {}
        for finding in static.report.findings:
            info = static.project.function_at(finding.file, finding.function)
            if info is not None:
                func_ids[finding.finding_id] = info.func_id
        correlated = correlate_findings(static.report.findings, func_ids, runtime, config)
        combined_graph = build_knowledge_graph(static.project, correlated)
        merge_runtime(combined_graph, runtime)
    return AppBundle(app_dir.name, app_dir, static, runtime, labels, static_graph, combined_graph, correlated)


def build_samples(bundle: AppBundle, builder: FeatureBuilder, use_runtime: bool) -> list[Sample]:
    if use_runtime and bundle.runtime is None:
        return []
    graph = bundle.combined_graph if use_runtime else bundle.static_graph
    runtime = bundle.runtime if use_runtime else None
    samples = []
    for symptom in bundle.labels.get("symptoms", []):
        tensors = builder.build(
            graph, symptom["endpoint"], symptom["kind"], bundle.static.project, runtime,
            root_causes=set(symptom["root_cause_functions"]),
        )
        if tensors is not None:
            samples.append(Sample(bundle.name, symptom["endpoint"], symptom["kind"], symptom.get("defect", "unknown"), tensors))
    return samples
