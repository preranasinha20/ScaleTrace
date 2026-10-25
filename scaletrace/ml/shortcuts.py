"""Shortcut-check suite: is the dataset learnable for the wrong reasons?

Pre-declared checks and thresholds (do not tune these after seeing results):

==========================  =====================================================  =============
check                       question                                               outcome
==========================  =====================================================  =============
single_feature              Does one static feature alone rank the root cause      FAIL >= 0.90
                            first? (static groups only; runtime signals are the
                            legitimate evidence we want the model to use)
node_only                   Does a node-only MLP (no graph) reach Top-1 >= 0.90    FAIL >= 0.90
                            on held-out data?
fingerprint                 Share of held-out root causes whose exact structural    FAIL >= 0.50
                            fingerprint occurs among training roots but never
                            among training non-roots (memorizable templates)
identifier_rename           Do GNN scores move when every project-defined name is   WARN > 0.05
                            replaced (code embedding reading names)?               (MRR change)
saturation                  Best validation score >= 0.98 at epoch <= 2?            WARN
near_duplicate              Max structural similarity between a test app and a      WARN >= 0.85
                            training app
==========================  =====================================================  =============

Verdict: FAIL if any check fails, otherwise PASS (warnings are reported but do not fail).
"""

from __future__ import annotations

import ast
import builtins
import textwrap
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scaletrace.ml.dataset import Sample
from scaletrace.ml.train import expected_rank

THRESHOLDS = {
    "single_feature_top1": 0.90,
    "node_only_top1": 0.90,
    "memorizable_fraction": 0.50,
    "rename_delta_mrr": 0.05,
    "saturation_epoch": 2,
    "saturation_score": 0.98,
    "near_duplicate": 0.85,
}
STATIC_GROUPS = ("type", "structure", "rules", "symptom", "code")
FINGERPRINT_GROUPS = ("structure", "rules")


@dataclass
class Check:
    name: str
    status: str  # pass | fail | warn | skip
    value: float | None
    threshold: float | None
    detail: str


@dataclass
class ShortcutReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "FAIL" if any(c.status == "fail" for c in self.checks) else "PASS"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "thresholds": THRESHOLDS, "checks": [vars(c) for c in self.checks]}

    def render(self) -> str:
        lines = [f"### Dataset validity: {self.verdict}", "", "| Check | Status | Value | Threshold | Detail |", "|---|---|---|---|---|"]
        for c in self.checks:
            value = "n/a" if c.value is None else f"{c.value:.3f}"
            threshold = "n/a" if c.threshold is None else f"{c.threshold:.2f}"
            lines.append(f"| {c.name} | {c.status.upper()} | {value} | {threshold} | {c.detail} |")
        return "\n".join(lines) + "\n"


def _usable(samples: list[Sample]) -> list[Sample]:
    return [s for s in samples if s.has_positive and s.tensors.candidates]


def _top1(scores: list[float], positives: list[bool]) -> bool:
    return expected_rank(scores, positives) <= 1.0


def single_feature_check(samples: list[Sample], names: list[str]) -> Check:
    usable = _usable(samples)
    columns = [i for i, n in enumerate(names) if n.split(":", 1)[0] in STATIC_GROUPS]
    best, best_name = 0.0, "-"
    for column in columns:
        for sign in (1.0, -1.0):
            hits = 0
            for sample in usable:
                t = sample.tensors
                scores = [sign * t.x[i][column] for i in t.candidates]
                positives = [t.labels[i] > 0 for i in t.candidates]
                hits += _top1(scores, positives)
            rate = hits / len(usable) if usable else 0.0
            if rate > best:
                best, best_name = rate, f"{'-' if sign < 0 else '+'}{names[column]}"
    status = "fail" if best >= THRESHOLDS["single_feature_top1"] else "pass"
    return Check("single_feature", status, best, THRESHOLDS["single_feature_top1"], f"best: {best_name} over {len(usable)} samples")


def node_only_check(top1: float | None, detail: str) -> Check:
    if top1 is None:
        return Check("node_only", "skip", None, THRESHOLDS["node_only_top1"], detail)
    status = "fail" if top1 >= THRESHOLDS["node_only_top1"] else "pass"
    return Check("node_only", status, top1, THRESHOLDS["node_only_top1"], detail)


def _fingerprint(row: list[float], columns: list[int]) -> tuple:
    return tuple(round(row[c], 4) for c in columns)


def fingerprint_check(train: list[Sample], test: list[Sample], names: list[str]) -> Check:
    columns = [i for i, n in enumerate(names) if n.split(":", 1)[0] in FINGERPRINT_GROUPS]
    if not columns:
        return Check("fingerprint", "skip", None, THRESHOLDS["memorizable_fraction"], "no structural features in this variant")
    counts: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # [roots, non-roots]
    for sample in _usable(train):
        t = sample.tensors
        for i in t.candidates:
            counts[_fingerprint(t.x[i], columns)][0 if t.labels[i] > 0 else 1] += 1
    roots = memorizable = 0
    for sample in _usable(test):
        t = sample.tensors
        for i in t.candidates:
            if t.labels[i] > 0:
                roots += 1
                root_count, other_count = counts.get(_fingerprint(t.x[i], columns), (0, 0))
                memorizable += root_count > 0 and other_count == 0
    fraction = memorizable / roots if roots else 0.0
    status = "fail" if fraction >= THRESHOLDS["memorizable_fraction"] else "pass"
    return Check("fingerprint", status, fraction, THRESHOLDS["memorizable_fraction"],
                 f"{memorizable}/{roots} held-out roots have a training-root-only structural fingerprint")


def rename_check(base_mrr: float | None, renamed_mrr: float | None) -> Check:
    if base_mrr is None or renamed_mrr is None:
        return Check("identifier_rename", "skip", None, THRESHOLDS["rename_delta_mrr"], "variant without code embedding")
    delta = abs(base_mrr - renamed_mrr)
    status = "warn" if delta > THRESHOLDS["rename_delta_mrr"] else "pass"
    return Check("identifier_rename", status, delta, THRESHOLDS["rename_delta_mrr"], f"MRR {base_mrr:.3f} -> {renamed_mrr:.3f} after renaming")


def saturation_check(histories: list[tuple[str, int, float]]) -> Check:
    saturated = [name for name, epoch, score in histories
                 if epoch <= THRESHOLDS["saturation_epoch"] and score >= THRESHOLDS["saturation_score"]]
    status = "warn" if saturated else "pass"
    return Check("saturation", status, len(saturated) / len(histories) if histories else None, None,
                 f"{len(saturated)}/{len(histories)} runs peaked at epoch <= {THRESHOLDS['saturation_epoch']} with validation MRR >= {THRESHOLDS['saturation_score']}")


def near_duplicate_check(train_dirs: list[Path], test_dirs: list[Path]) -> Check:
    from scaletrace.benchmark.validate import _jaccard, _shingles

    train = [_shingles(p) for p in train_dirs]
    best = 0.0
    for test_dir in test_dirs:
        shingles = _shingles(test_dir)
        best = max([best] + [_jaccard(shingles, t) for t in train])
    status = "warn" if best >= THRESHOLDS["near_duplicate"] else "pass"
    return Check("near_duplicate", status, best, THRESHOLDS["near_duplicate"],
                 f"max structural similarity of {len(test_dirs)} test apps to {len(train_dirs)} training apps")


# --- identifier renaming (for the rename check) --------------------------------------------------

_BUILTINS = set(dir(builtins))


def project_names(project) -> set[str]:
    """Identifiers defined by the project: functions, classes, module names, parameters, locals,
    class attributes. Library and API names (db.query, sorted, json.dumps) are not included."""
    names: set[str] = set()
    for module, ctx in project.modules.items():
        names.update(module.split("."))
        for node in ast.walk(ctx.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names - _BUILTINS - {"self", "cls"}


class _Renamer(ast.NodeTransformer):
    def __init__(self, mapping) -> None:
        self.mapping = mapping

    def visit_Name(self, node):
        node.id = self.mapping(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self.mapping(node.arg)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = self.mapping(node.attr)
        return node

    def visit_keyword(self, node):
        self.generic_visit(node)
        if node.arg:
            node.arg = self.mapping(node.arg)
        return node

    def _named(self, node):
        self.generic_visit(node)
        node.name = self.mapping(node.name)
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _named


def make_renamer(seed: int = 0):
    """Returns source_transform(source, project) replacing project-defined identifiers consistently."""
    cache: dict[int, set[str]] = {}

    def transform(source: str, project) -> str:
        defined = cache.setdefault(id(project), project_names(project))

        def mapping(name: str) -> str:
            if name not in defined:
                return name
            return f"n{zlib.crc32(f'{seed}:{name}'.encode()) % 1_000_000:06d}"

        try:
            tree = ast.parse(textwrap.dedent(source))
        except SyntaxError:
            return source
        return ast.unparse(_Renamer(mapping).visit(tree))

    return transform
