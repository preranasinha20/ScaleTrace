"""Ground-truth files for benchmark applications, and matching findings against them.

A benchmark application ships a ``scaletrace_groundtruth.json``::

    {
      "groundtruth_version": "0.1",
      "application": "orders_app_defective",
      "rules_in_scope": ["DB-001"],
      "expected_findings": [
        {"rule_id": "DB-001", "file": "app/main.py", "function": "list_order_summaries", "line": 42}
      ]
    }

Only findings for ``rules_in_scope`` are scored, so a benchmark can be labelled for some rules
before every rule is implemented. A finding matches an expectation when rule, file and
function agree and the line is within ``line_tolerance``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from scaletrace.schemas import Finding


class ExpectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    file: str
    function: str
    line: int
    line_end: int | None = None  # when set, any finding within [line, line_end] matches
    note: str = ""

    def covers(self, line: int, tolerance: int) -> bool:
        if self.line_end is not None:
            return self.line <= line <= self.line_end
        return abs(line - self.line) <= tolerance


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groundtruth_version: str
    application: str
    description: str = ""
    rules_in_scope: list[str]
    expected_findings: list[ExpectedFinding] = Field(default_factory=list)


@dataclass
class MatchResult:
    true_positives: list[tuple[ExpectedFinding, Finding]] = field(default_factory=list)
    false_positives: list[Finding] = field(default_factory=list)
    false_negatives: list[ExpectedFinding] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        found = len(self.true_positives) + len(self.false_positives)
        return len(self.true_positives) / found if found else None

    @property
    def recall(self) -> float | None:
        expected = len(self.true_positives) + len(self.false_negatives)
        return len(self.true_positives) / expected if expected else None


def load_groundtruth(path: str | Path) -> GroundTruth:
    return GroundTruth.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def match_findings(findings: list[Finding], truth: GroundTruth, line_tolerance: int = 0) -> MatchResult:
    result = MatchResult()
    candidates = [f for f in findings if f.rule_id in truth.rules_in_scope]
    unmatched = list(candidates)
    for expected in truth.expected_findings:
        hit = next(
            (
                f for f in unmatched
                if f.rule_id == expected.rule_id
                and f.file == expected.file
                and f.function == expected.function
                and expected.covers(f.line, line_tolerance)
            ),
            None,
        )
        if hit is None:
            result.false_negatives.append(expected)
        else:
            unmatched.remove(hit)
            result.true_positives.append((expected, hit))
    result.false_positives = unmatched
    return result
