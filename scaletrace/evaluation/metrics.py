"""Detection metrics (precision / recall / F1) over one or more benchmark applications."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scaletrace.evaluation.groundtruth import GroundTruth, MatchResult, match_findings
from scaletrace.rules.rulebook import Rulebook
from scaletrace.schemas import Finding

GROUNDTRUTH_FILE = "scaletrace_groundtruth.json"


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class EvaluationResult:
    per_app: dict[str, MatchResult] = field(default_factory=dict)
    per_rule: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    per_category: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    overall: Counts = field(default_factory=Counts)

    def rows(self) -> list[dict[str, object]]:
        def row(level: str, name: str, c: Counts) -> dict[str, object]:
            return {"level": level, "name": name, "tp": c.tp, "fp": c.fp, "fn": c.fn,
                    "precision": c.precision, "recall": c.recall, "f1": c.f1}

        rows = [row("rule", k, v) for k, v in sorted(self.per_rule.items())]
        rows += [row("category", k, v) for k, v in sorted(self.per_category.items())]
        rows.append(row("overall", "all", self.overall))
        return rows


def evaluate(results: dict[str, tuple[list[Finding], GroundTruth]], rulebook: Rulebook) -> EvaluationResult:
    """``results`` maps an application name to (findings, ground truth)."""
    category = {rule.id: rule.category for rule in rulebook.rules}
    evaluation = EvaluationResult()
    for app, (findings, truth) in results.items():
        match = match_findings(findings, truth)
        evaluation.per_app[app] = match
        for _, finding in match.true_positives:
            for counts in (evaluation.per_rule[finding.rule_id], evaluation.per_category[category[finding.rule_id]], evaluation.overall):
                counts.tp += 1
        for finding in match.false_positives:
            for counts in (evaluation.per_rule[finding.rule_id], evaluation.per_category[category[finding.rule_id]], evaluation.overall):
                counts.fp += 1
        for expected in match.false_negatives:
            for counts in (evaluation.per_rule[expected.rule_id], evaluation.per_category[category.get(expected.rule_id, "unknown")], evaluation.overall):
                counts.fn += 1
    return evaluation


def write_csv(evaluation: EvaluationResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = evaluation.rows()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def render_table(evaluation: EvaluationResult) -> str:
    fmt = lambda v: "  n/a" if v is None else f"{v:5.2f}"
    lines = [f"{'level':<9} {'name':<28} {'TP':>4} {'FP':>4} {'FN':>4} {'prec':>6} {'rec':>6} {'F1':>6}"]
    for row in evaluation.rows():
        lines.append(
            f"{row['level']:<9} {row['name']:<28} {row['tp']:>4} {row['fp']:>4} {row['fn']:>4} "
            f"{fmt(row['precision']):>6} {fmt(row['recall']):>6} {fmt(row['f1']):>6}"
        )
    return "\n".join(lines)
