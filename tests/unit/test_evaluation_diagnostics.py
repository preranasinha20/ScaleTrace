"""Evaluation diagnostics added on 2026-10-29: FP breakdown, reviewed-only sensitivity, app-level bootstrap.

These never change the official TP/FP/FN rows; they only explain them (docs/evaluation_metrics_audit.md).
"""

from __future__ import annotations

from types import SimpleNamespace

from scaletrace.benchmark.final import app_bootstrap, fp_breakdown, reviewed_only
from scaletrace.evaluation.groundtruth import ExpectedFinding, MatchResult
from scaletrace.evaluation.metrics import Counts, EvaluationResult


def hit(rule="MEM-001", file="app.py", line=10, function="f", loop_line=None):
    details = {"innermost_loop": {"line": loop_line}} if loop_line is not None else {}
    return SimpleNamespace(rule_id=rule, file=file, line=line, function=function,
                           static_evidence=[SimpleNamespace(details=details)])


def evaluation_with(per_app: dict[str, MatchResult]) -> EvaluationResult:
    result = EvaluationResult(per_app=per_app)
    result.overall = Counts(
        tp=sum(len(m.true_positives) for m in per_app.values()),
        fp=sum(len(m.false_positives) for m in per_app.values()),
        fn=sum(len(m.false_negatives) for m in per_app.values()),
    )
    return result


def manifest(*known):
    return SimpleNamespace(known_non_defects=[SimpleNamespace(rule_id=r, file=f, line=n) for r, f, n in known])


def test_fp_breakdown_separates_reviewed_from_unreviewed():
    expected = ExpectedFinding(rule_id="MEM-001", file="app.py", function="f", line=5)
    a = MatchResult(true_positives=[(expected, hit(line=5))], false_positives=[hit(line=10), hit(line=20)])
    b = MatchResult(false_positives=[hit(rule="DB-001", line=3)])
    evaluation = evaluation_with({"a": a, "b": b})
    manifests = {"a": manifest(("MEM-001", "app.py", 10)), "b": manifest()}
    breakdown = fp_breakdown(evaluation, manifests)
    assert breakdown["total"] == {"reviewed_non_defect": 1, "not_reviewed": 2, "same_loop_as_true_positive": 0}
    assert breakdown["per_app"]["a"] == {"reviewed_non_defect": 1, "not_reviewed": 1, "same_loop_as_true_positive": 0}
    # A known non-defect must match rule, file AND line; a different rule at the same line is not reviewed.
    assert fp_breakdown(evaluation_with({"a": MatchResult(false_positives=[hit(rule="DB-001", line=10)])}),
                        {"a": manifests["a"]})["total"]["reviewed_non_defect"] == 0


def test_reviewed_only_sensitivity_uses_the_smaller_denominator():
    expected = [ExpectedFinding(rule_id="MEM-001", file="app.py", function="f", line=n) for n in (1, 2, 3)]
    a = MatchResult(true_positives=[(expected[0], hit(line=1)), (expected[1], hit(line=2))],
                    false_positives=[hit(line=10), hit(line=11), hit(line=12)],
                    false_negatives=[expected[2]])
    evaluation = evaluation_with({"a": a})
    sens = reviewed_only(evaluation, {"a": manifest(("MEM-001", "app.py", 10))})
    assert (sens["tp"], sens["fp_reviewed"], sens["fn"]) == (2, 1, 1)
    assert abs(sens["precision"] - 2 / 3) < 1e-9 and abs(sens["recall"] - 2 / 3) < 1e-9
    assert evaluation.overall.precision == 2 / 5  # the official precision is untouched


def test_reviewed_only_handles_zero_precision():
    a = MatchResult(false_positives=[hit(line=10)],
                    false_negatives=[ExpectedFinding(rule_id="MEM-001", file="app.py", function="f", line=1)])
    sens = reviewed_only(evaluation_with({"a": a}), {"a": manifest(("MEM-001", "app.py", 10))})
    assert sens["precision"] == 0.0 and sens["recall"] == 0.0 and sens["f1"] is None


def test_app_bootstrap_is_deterministic_and_brackets_the_pooled_value():
    per_app = {"a": (3, 0, 0), "b": (0, 5, 1), "c": (2, 2, 2), "d": (1, 1, 0)}
    first, second = app_bootstrap(per_app, n=500, seed=1), app_bootstrap(per_app, n=500, seed=1)
    assert first == second and first["apps"] == 4
    pooled_precision = 6 / 14
    low, high = first["precision"]
    assert low <= pooled_precision <= high
    assert app_bootstrap({}) == {}


def test_app_bootstrap_without_positives_has_no_recall():
    ci = app_bootstrap({"a": (0, 4, 0), "b": (0, 1, 0)}, n=100)
    assert ci["recall"] is None and ci["precision"] == [0.0, 0.0]


def test_same_loop_companions_counts_extra_queries_in_a_matched_loop():
    expected = ExpectedFinding(rule_id="DB-001", file="svc.py", function="send", line=20)
    tp = hit(rule="DB-001", file="svc.py", line=20, function="send", loop_line=18)
    same_loop = hit(rule="DB-001", file="svc.py", line=22, function="send", loop_line=18)
    other_loop = hit(rule="DB-001", file="svc.py", line=40, function="send", loop_line=38)
    other_function = hit(rule="DB-001", file="svc.py", line=22, function="receive", loop_line=18)
    other_rule = hit(rule="CMP-001", file="svc.py", line=21, function="send", loop_line=18)
    no_loop = hit(rule="MEM-001", file="svc.py", line=23, function="send")
    match = MatchResult(true_positives=[(expected, tp)],
                        false_positives=[same_loop, other_loop, other_function, other_rule, no_loop])
    breakdown = fp_breakdown(evaluation_with({"a": match}), {"a": manifest()})
    assert breakdown["total"]["same_loop_as_true_positive"] == 1
    # The official counts are untouched: all five remain false positives.
    assert len(match.false_positives) == 5 and breakdown["total"]["not_reviewed"] == 5
