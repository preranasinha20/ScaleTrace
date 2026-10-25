"""Static analysis pipeline: ingestion -> parsing -> project index -> detectors -> report."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scaletrace import __version__
from scaletrace.analyzer.ingestion import discover_repository
from scaletrace.analyzer.module import ModuleContext, parse_module
from scaletrace.analyzer.project import ProjectIndex
from scaletrace.rules.config import DetectorConfig
from scaletrace.rules.detectors import DETECTORS, RawMatch
from scaletrace.rules.rulebook import Rulebook, RuleSpec, load_rulebook
from scaletrace.schemas import AnalysisReport, Finding, FindingStatus

STATUS_PHRASES = {
    FindingStatus.STATIC_CANDIDATE: "pending (no runtime evidence yet)",
    FindingStatus.RUNTIME_OBSERVED: "observed at runtime without a static match",
    FindingStatus.CORRELATED_FINDING: "supported by runtime evidence",
    FindingStatus.NOT_CONFIRMED: "not confirmed by the available runtime evidence",
}

STATIC_LIMITATIONS = [
    "Static analysis only: findings are potential scalability risks, not observed failures.",
    "Database handles are inferred from annotations, FastAPI dependencies, factory calls and, "
    "as a fallback, variable names; the evidence records which basis was used.",
    "The call graph is approximate: calls through dynamic dispatch, callbacks or objects of unknown "
    "type are not resolved, so indirect findings and endpoint mappings can be incomplete.",
    "Loop cardinality is not inferred, except that loops over small literal collections or constant "
    "range() calls are treated as bounded and not reported.",
]
MAX_ENDPOINTS_IN_LABEL = 3


class UnknownRuleError(ValueError):
    """A requested rule ID is not in the rulebook or has no detector."""


@dataclass
class StaticAnalysis:
    """Everything the static phase produces; later phases build on the project index."""

    report: AnalysisReport
    project: ProjectIndex


def _human_category(category: str) -> str:
    return category.replace("_", " ")


def build_finding(rule: RuleSpec, match: RawMatch, project: ProjectIndex) -> Finding:
    node = match.node
    info = match.info
    line = getattr(node, "lineno", 1)
    column = getattr(node, "col_offset", None)
    status = FindingStatus.STATIC_CANDIDATE
    endpoints = project.endpoints_reaching(info.func_id)
    label = ", ".join(endpoints[:MAX_ENDPOINTS_IN_LABEL])
    if len(endpoints) > MAX_ENDPOINTS_IN_LABEL:
        label += f" (+{len(endpoints) - MAX_ENDPOINTS_IN_LABEL} more)"
    message = (
        f"Potential {_human_category(rule.category)} risk: {rule.name}. "
        f"Static evidence found at {info.file}:{line} ({match.summary}). "
        f"Runtime confirmation is {STATUS_PHRASES[status]}."
    )
    return Finding(
        finding_id=f"{rule.id}:{info.file}:{line}:{column}",
        rule_id=rule.id,
        rule_name=rule.name,
        category=rule.category,
        severity=rule.severity,
        classification=rule.classification,
        file=info.file,
        line=line,
        end_line=getattr(node, "end_lineno", None),
        column=column,
        function=info.qualname,
        endpoint=label or None,
        reachable_endpoints=endpoints,
        message=message,
        static_evidence=match.evidence,
        status=status,
        possible_false_positives=list(rule.false_positives),
        verification_steps=list(rule.diagnostic_steps),
    )


def select_rules(rulebook: Rulebook, rule_ids: Iterable[str] | None) -> list[str]:
    implemented = [rule_id for rule_id in rulebook.rule_ids if rule_id in DETECTORS]
    if rule_ids is None:
        return implemented
    selected = []
    for rule_id in rule_ids:
        if rule_id not in rulebook.rule_ids:
            raise UnknownRuleError(f"{rule_id} is not defined in rulebook {rulebook.rulebook_version}")
        if rule_id not in DETECTORS:
            raise UnknownRuleError(f"{rule_id} has no detector yet")
        selected.append(rule_id)
    return selected


def run_static_analysis(
    root: str | Path,
    *,
    rulebook: Rulebook | None = None,
    config: DetectorConfig | None = None,
    rule_ids: Iterable[str] | None = None,
) -> StaticAnalysis:
    """Statically analyze a local repository. Target code is parsed, never imported or run."""
    rulebook = rulebook or load_rulebook()
    config = config or DetectorConfig()
    selected = select_rules(rulebook, rule_ids)

    inventory = discover_repository(root, config)
    skipped = list(inventory.skipped)
    modules: list[ModuleContext] = []
    for source_file in inventory.python_files:
        parsed = parse_module(source_file)
        if isinstance(parsed, ModuleContext):
            modules.append(parsed)
        else:
            skipped.append(parsed)

    project = ProjectIndex(modules, config)
    findings: list[Finding] = []
    seen: set[str] = set()
    for rule_id in selected:
        rule = rulebook.get(rule_id)
        for match in DETECTORS[rule_id](config).detect(project):
            finding = build_finding(rule, match, project)
            if finding.finding_id not in seen:
                seen.add(finding.finding_id)
                findings.append(finding)

    findings.sort(key=lambda f: (f.file, f.line, f.column or 0, f.rule_id))
    report = AnalysisReport(
        scaletrace_version=__version__,
        rulebook_version=rulebook.rulebook_version,
        rulebook_status=rulebook.status,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        repository=inventory.root.name,
        files_analyzed=len(modules),
        files_skipped=skipped,
        config_files=inventory.config_files,
        rules_applied=selected,
        rules_not_implemented=[rule_id for rule_id in rulebook.rule_ids if rule_id not in DETECTORS],
        findings=findings,
        limitations=STATIC_LIMITATIONS,
    )
    return StaticAnalysis(report=report, project=project)


def analyze_repository(root: str | Path, **kwargs) -> AnalysisReport:
    """Convenience wrapper returning only the report (see ``run_static_analysis``)."""
    return run_static_analysis(root, **kwargs).report
