"""Static validation of a benchmark (manifests, labels, splits, lock). Never executes application code.

Errors make the benchmark unusable for evaluation; warnings need human review. Each issue has a
stable ``code`` so tests and reports can refer to it.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from scaletrace.benchmark.loader import (
    ManifestError,
    file_sha256,
    find_manifest_dirs,
    load_index,
    load_manifest,
    positive_defects,
    source_sha256,
    unverified_defects,
)
from scaletrace.benchmark.schema import MANIFEST_FILE, AppManifest
from scaletrace.benchmark.split import compute_groups
from scaletrace.graph.knowledge_graph import CONTROL_EDGES, build_knowledge_graph
from scaletrace.pipeline import run_static_analysis
from scaletrace.rules.detectors import DETECTORS
from scaletrace.rules.rulebook import Rulebook

NEAR_DUPLICATE_THRESHOLD = 0.85
SHINGLE = 6


@dataclass
class Issue:
    level: str  # "error" | "warning"
    app_id: str
    code: str
    message: str


@dataclass
class AppSummary:
    app_id: str
    path: Path
    kind: str
    split: str
    defects: int
    verified: int
    unverified: int
    rejected: int


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    apps: list[AppSummary] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    def codes(self, level: str | None = None) -> set[str]:
        return {i.code for i in self.issues if level is None or i.level == level}


def _shingles(app_dir: Path) -> set[int]:
    """Structure-only shingles (AST node-type sequence), robust to renaming."""
    sequence: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        # Real repositories contain oddities such as a *directory* named "something.py".
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(app_dir).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        sequence.extend(type(node).__name__ for node in ast.walk(tree))
    return {hash(tuple(sequence[i:i + SHINGLE])) for i in range(max(0, len(sequence) - SHINGLE + 1))}


def _jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _validate_app(app_dir: Path, manifest: AppManifest, rulebook: Rulebook, report: ValidationReport) -> None:
    def issue(level: str, code: str, message: str) -> None:
        report.issues.append(Issue(level, manifest.app_id, code, message))

    if manifest.kind == "bad" and not manifest.defects:
        issue("error", "bad_without_defects", "kind 'bad' but no defects are listed")
    if manifest.kind == "good":
        for defect in manifest.defects:
            if not defect.description.strip():
                issue("error", "good_defect_unjustified", f"{defect.defect_id}: defects in a good app need a description")
    seen: Counter = Counter(d.defect_id for d in manifest.defects)
    for defect_id, n in seen.items():
        if n > 1:
            issue("error", "duplicate_defect_id", f"{defect_id} appears {n} times")
    for defect in manifest.defects:
        v = defect.verification
        if v.rejected:
            continue
        if not v.source_confirmed:
            issue("warning", "defect_unverified", f"{defect.defect_id}: not source-confirmed; excluded from positives at both tiers")
        elif not v.fully_verified:
            issue("warning", "defect_source_only", f"{defect.defect_id}: source-confirmed only; counts at the source tier, not the runtime tier")

    static = run_static_analysis(app_dir)
    project = static.project
    graph = build_knowledge_graph(project, static.report.findings)
    endpoints = set(project.endpoints())
    rule_ids = set(rulebook.rule_ids)

    for defect in manifest.defects:
        tag = defect.defect_id
        if defect.rule_id not in rule_ids:
            issue("error", "unknown_rule", f"{tag}: rule {defect.rule_id} is not in rulebook {rulebook.rulebook_version}")
        elif rulebook.get(defect.rule_id).category != defect.category:
            issue("error", "category_mismatch", f"{tag}: category {defect.category!r} != rulebook {rulebook.get(defect.rule_id).category!r}")
        if not (app_dir / defect.file).is_file():
            issue("error", "file_missing", f"{tag}: {defect.file} does not exist")
            continue
        info = project.function_at(defect.file, defect.function)
        if info is None:
            issue("error", "function_missing", f"{tag}: function {defect.function!r} not found in {defect.file}")
        elif info.end_line is not None and not (info.line <= defect.line_start and defect.line_end <= info.end_line):
            issue("error", "line_outside_function", f"{tag}: lines {defect.line_start}-{defect.line_end} are outside {defect.function} ({info.line}-{info.end_line})")
        if defect.endpoint is not None and defect.endpoint not in endpoints:
            issue("error", "endpoint_missing", f"{tag}: endpoint {defect.endpoint!r} not found among {len(endpoints)} routes")
        for func_id in defect.root_cause_functions:
            if func_id not in project.functions:
                issue("error", "root_cause_missing", f"{tag}: root-cause function {func_id!r} not found")
        for node in defect.path:
            if node not in graph:
                issue("warning", "path_node_missing", f"{tag}: path node {node!r} not in the knowledge graph")
        for source, target in zip(defect.path, defect.path[1:]):
            if source in graph and target in graph and not graph.has_edge(source, target):
                issue("warning", "path_edge_missing", f"{tag}: no static edge {source} -> {target} (runtime-observed edges may add it)")
        if defect.verification.source_confirmed and defect.rule_id in DETECTORS:
            hit = any(
                f.rule_id == defect.rule_id and f.file == defect.file and defect.line_start <= f.line <= defect.line_end
                for f in static.report.findings
            )
            if not hit:
                issue("warning", "rule_misses_defect", f"{tag}: ScaleTrace's {defect.rule_id} detector does not report this verified defect")

    if manifest.kind == "good":
        documented = {(k.rule_id, k.file, k.line) for k in manifest.known_non_defects}
        labelled = {(d.rule_id, d.file) for d in manifest.defects}
        for finding in static.report.findings:
            key = (finding.rule_id, finding.file, finding.line)
            if key not in documented and (finding.rule_id, finding.file) not in labelled:
                issue("warning", "unreviewed_rule_hit", f"{finding.rule_id} at {finding.file}:{finding.line} on a good app is neither a defect nor a known non-defect")

    verified = len(positive_defects(manifest, "source"))
    unverified = len(unverified_defects(manifest, "source"))
    rejected = sum(1 for d in manifest.defects if d.verification.rejected)
    report.apps.append(AppSummary(manifest.app_id, app_dir, manifest.kind, manifest.split, len(manifest.defects), verified, unverified, rejected))


def validate_benchmark(root: str | Path, rulebook: Rulebook, check_duplicates: bool = True) -> ValidationReport:
    root = Path(root)
    report = ValidationReport()
    loaded: dict[str, tuple[Path, AppManifest]] = {}
    for app_dir in find_manifest_dirs(root):
        try:
            manifest = load_manifest(app_dir)
        except ManifestError as exc:
            report.issues.append(Issue("error", app_dir.name, "schema_invalid", str(exc)))
            continue
        if manifest.app_id in loaded:
            report.issues.append(Issue("error", manifest.app_id, "duplicate_app_id", f"also used by {loaded[manifest.app_id][0]}"))
            continue
        loaded[manifest.app_id] = (app_dir, manifest)

    defect_owner: dict[str, str] = {}
    for app_id, (app_dir, manifest) in sorted(loaded.items()):
        for defect in manifest.defects:
            if defect.defect_id in defect_owner and defect_owner[defect.defect_id] != app_id:
                report.issues.append(Issue("error", app_id, "duplicate_defect_id", f"{defect.defect_id} also in {defect_owner[defect.defect_id]}"))
            defect_owner.setdefault(defect.defect_id, app_id)
        if manifest.origin.derived_from and manifest.origin.derived_from not in loaded:
            report.issues.append(Issue("error", app_id, "derived_from_unknown", f"derived_from {manifest.origin.derived_from!r} is not a known app_id"))
        if manifest.origin.controlled:
            if not manifest.origin.derived_from:
                report.issues.append(Issue("error", app_id, "controlled_without_base", "controlled copy must name its base app in derived_from"))
            elif manifest.origin.derived_from in loaded and loaded[manifest.origin.derived_from][1].split == "excluded" \
                    and manifest.split != "excluded":
                report.issues.append(Issue("error", app_id, "controlled_base_excluded",
                                           f"base app {manifest.origin.derived_from} is excluded; exclude this copy too"))
        _validate_app(app_dir, manifest, rulebook, report)

    index = load_index(root)
    if index is not None:
        _validate_index(root, index, loaded, report)
    else:
        manifests = {a: m for a, (_, m) in loaded.items()}
        groups = compute_groups(manifests, ["template_family", "derived_from"])
        _check_group_splits(groups, {a: m.split for a, m in manifests.items()}, report)

    if check_duplicates and len(loaded) > 1:
        shingles = {a: _shingles(p) for a, (p, _) in loaded.items()}
        splits = {e.app_id: e.split for e in index.apps} if index else {a: m.split for a, (_, m) in loaded.items()}
        ids = sorted(loaded)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                similarity = _jaccard(shingles[a], shingles[b])
                if similarity >= NEAR_DUPLICATE_THRESHOLD and splits.get(a) != splits.get(b):
                    report.issues.append(Issue("warning", a, "near_duplicate", f"structurally {similarity:.2f} similar to {b} in a different split"))
    return report


def _check_group_splits(groups: dict[str, str], splits: dict[str, str], report: ValidationReport) -> None:
    by_group: dict[str, set[str]] = {}
    for app_id, group in groups.items():
        if splits.get(app_id) in ("dev", "test"):
            by_group.setdefault(group, set()).add(splits[app_id])
    for group, used in by_group.items():
        if len(used) > 1:
            members = sorted(a for a, g in groups.items() if g == group)
            report.issues.append(Issue("error", members[0], "split_group_violation", f"related apps {members} are split across {sorted(used)}"))


def _validate_index(root: Path, index, loaded: dict, report: ValidationReport) -> None:
    on_disk = set(loaded)
    listed = {e.app_id for e in index.apps}
    for app_id in sorted(on_disk - listed):
        report.issues.append(Issue("error", app_id, "not_in_index", "application has a manifest but is not in benchmark_index.json"))
    for app_id in sorted(listed - on_disk):
        report.issues.append(Issue("error", app_id, "index_entry_missing", "listed in the index but no manifest was found"))
    for entry in index.apps:
        if entry.app_id in loaded:
            manifest = loaded[entry.app_id][1]
            if manifest.split not in ("unassigned", entry.split):
                report.issues.append(Issue("error", entry.app_id, "split_mismatch", f"manifest says {manifest.split!r}, index says {entry.split!r}"))
    manifests = {a: m for a, (_, m) in loaded.items()}
    groups = compute_groups(manifests, index.group_by)
    _check_group_splits(groups, {e.app_id: e.split for e in index.apps}, report)

    if index.lock is not None:
        tests = {e.app_id for e in index.apps if e.split == "test"}
        if tests != set(index.lock.apps):
            report.issues.append(Issue("error", "-", "lock_violation", f"test apps changed since lock: added {sorted(tests - set(index.lock.apps))}, removed {sorted(set(index.lock.apps) - tests)}"))
        for app_id, locked in index.lock.apps.items():
            if app_id not in loaded:
                continue
            app_dir = loaded[app_id][0]
            if file_sha256(app_dir / MANIFEST_FILE) != locked.manifest_sha256:
                report.issues.append(Issue("error", app_id, "lock_violation", "manifest changed since the test set was locked"))
            if source_sha256(app_dir) != locked.source_sha256:
                report.issues.append(Issue("error", app_id, "lock_violation", "source files changed since the test set was locked"))
