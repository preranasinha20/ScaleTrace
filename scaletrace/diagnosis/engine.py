"""End-to-end diagnosis: static analysis + optional runtime report -> graph -> diagnoses."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import networkx as nx

from scaletrace import __version__
from scaletrace.diagnosis.backtrack import Backtracker, Ranker
from scaletrace.diagnosis.config import DiagnosisConfig
from scaletrace.diagnosis.correlate import correlate_findings
from scaletrace.diagnosis.symptoms import detect_symptoms, group_by_endpoint
from scaletrace.graph.knowledge_graph import build_knowledge_graph, merge_runtime
from scaletrace.pipeline import StaticAnalysis
from scaletrace.schemas.diagnosis import DiagnosisReport, RuntimeObservation, Symptom
from scaletrace.schemas.runtime import RuntimeReport

DIAGNOSIS_LIMITATIONS = [
    "Diagnoses rank candidate code with a proposed heuristic (Risk(v)); the top candidate is a probable "
    "cause, not proven causality.",
    "Without request-level traces, paths come from the approximate call graph plus observed call edges; "
    "diagnoses lacking runtime attribution are marked approximate.",
]


def user_symptom(endpoint: str | None = None, resource: str | None = None, description: str | None = None) -> Symptom:
    """A symptom reported by the user (e.g. from production monitoring), not measured here."""
    target = endpoint or resource or "unknown"
    return Symptom(
        symptom_id=f"user_reported:{target}",
        kind="user_reported",
        endpoint=endpoint,
        resource=resource,
        description=description or f"Reported by the user for {target} (not measured by ScaleTrace).",
    )


def diagnose_repository(
    static: StaticAnalysis,
    runtime: RuntimeReport | None = None,
    config: DiagnosisConfig | None = None,
    reported: list[Symptom] | None = None,
    ranker: Ranker | None = None,
    ranker_name: str = "gnn",
) -> tuple[DiagnosisReport, nx.MultiDiGraph]:
    config = config or DiagnosisConfig()
    report = static.report.model_copy(deep=True)
    project = static.project

    usable_runtime = runtime if runtime is not None and runtime.status == "completed" else None
    if usable_runtime is not None:
        func_ids = {}
        for finding in report.findings:
            info = project.function_at(finding.file, finding.function)
            if info is not None:
                func_ids[finding.finding_id] = info.func_id
        report.findings = correlate_findings(report.findings, func_ids, usable_runtime, config)

    graph = build_knowledge_graph(project, report.findings)
    if usable_runtime is not None:
        merge_runtime(graph, usable_runtime)

    symptoms = detect_symptoms(usable_runtime, config) if usable_runtime is not None else []
    symptoms += reported or []
    backtracker = Backtracker(graph, report.findings, usable_runtime, config, ranker=ranker, ranker_name=ranker_name)
    diagnoses = []
    observations = []
    for primary, supporting in group_by_endpoint(symptoms):
        diagnosis = backtracker.diagnose(primary, supporting)
        if diagnosis is None:
            observations.append(RuntimeObservation(symptom=primary, note="The symptom's endpoint or resource is not in the knowledge graph."))
            continue
        diagnoses.append(diagnosis)
        if diagnosis.status == "unexplained":
            observations.append(RuntimeObservation(symptom=primary, note=diagnosis.probable_cause))

    limitations = list(report.limitations) + DIAGNOSIS_LIMITATIONS
    if runtime is not None:
        limitations += runtime.limitations + [f"Unavailable runtime metric: {m}." for m in runtime.unavailable_metrics]
        if runtime.status != "completed":
            limitations.append(f"The runtime run failed ({runtime.error}); only static evidence was used.")
    return (
        DiagnosisReport(
            scaletrace_version=__version__,
            repository=report.repository,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            runtime_available=usable_runtime is not None,
            scoring=config.model_dump(),
            static_report=report,
            symptoms=symptoms,
            diagnoses=diagnoses,
            unexplained_observations=observations,
            findings_by_status=dict(Counter(f.status.value for f in report.findings)),
            limitations=list(dict.fromkeys(limitations)),
        ),
        graph,
    )
