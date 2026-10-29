"""The final benchmark evaluation (run once, on the locked test split) and its dev-split rehearsal.

Preconditions (checked, not assumed): the benchmark validates without errors, the index is locked, and
every test application passes the lock check. The evaluation is static-only -- public repositories are
never executed (runtime testing needs the sandbox) -- so it reports:

1. Detection: precision / recall / F1 of the rule engine against fully verified defects, per rule,
   category and overall; unverified defects are counted separately and never as positives.
2. Root-cause ranking for verified defects that name an endpoint, a symptom and a root-cause function:
   random, proximity, rules_only and heuristic_static, plus any given model checkpoints. Models that
   expect runtime features are run without them and are marked as such.

``dry_run=True`` evaluates the dev split instead (diagnostic access) and does not log a final run.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scaletrace.benchmark.guard import check_access, record_final_run
from scaletrace.benchmark.loader import groundtruth_from_manifest, load_index, load_manifest, unverified_defects
from scaletrace.benchmark.schema import SCORING_LEVELS
from scaletrace.benchmark.validate import validate_benchmark
from scaletrace.rules.rulebook import Rulebook


RANKING_LEVELS = ("recorded", "source")  # recorded labels and source-confirmed labels, reported separately


class FinalEvaluationError(RuntimeError):
    pass


def run_final(root: str | Path, rulebook: Rulebook, model_paths: list[str | Path], out_dir: str | Path,
              dry_run: bool = False, log=print) -> dict:
    from scaletrace.evaluation.metrics import evaluate
    from scaletrace.ml.evaluate import summarize
    from scaletrace.ml.experiment import baseline_records, load_bundles, model_records
    from scaletrace.ml.train import load_checkpoint
    from scaletrace.pipeline import run_static_analysis

    root = Path(root)
    index = load_index(root)
    if index is None or index.lock is None:
        raise FinalEvaluationError(f"{root}: benchmark index missing or not locked; run `benchmark split` and `benchmark lock`")
    report = validate_benchmark(root, rulebook)
    if report.errors:
        codes = Counter(i.code for i in report.errors)
        raise FinalEvaluationError(f"benchmark has validation errors: {dict(codes)}")

    split = "dev" if dry_run else "test"
    apps = [root / e.path for e in index.apps if e.split == split]
    if not apps:
        raise FinalEvaluationError(f"no applications in the {split} split")
    check_access(apps, "diagnostic" if dry_run else "final")

    # Controlled copies (team-injected defects) are scored separately and never mixed into real-world results.
    detection_inputs: dict[str, dict[str, dict]] = {subset: {level: {} for level in SCORING_LEVELS} for subset in ("real", "controlled")}
    pending, real_apps, controlled_apps = {}, [], []
    pending_hits: dict[str, dict[str, int]] = {level: {} for level in SCORING_LEVELS}
    manifests = {}
    for app in apps:
        manifest = load_manifest(app)
        manifests[manifest.app_id] = manifest
        subset = "controlled" if manifest.origin.controlled else "real"
        (controlled_apps if manifest.origin.controlled else real_apps).append(app)
        static = run_static_analysis(app).report
        for level in SCORING_LEVELS:
            scored, held = split_pending(static.findings, manifest, level)
            pending_hits[level][manifest.app_id] = len(held)
            detection_inputs[subset][level][manifest.app_id] = (scored, groundtruth_from_manifest(manifest, static.rules_applied, level))
        pending[manifest.app_id] = len(unverified_defects(manifest, "source"))
    detection = {level: evaluate(detection_inputs["real"][level], rulebook) for level in SCORING_LEVELS}
    detection_controlled = {level: evaluate(detection_inputs["controlled"][level], rulebook)
                            for level in SCORING_LEVELS} if controlled_apps else {}

    # Static-only benchmark (runtime verification needs the sandbox). Ranking per label tier, never mixed.
    model_notes, ranking, ranking_samples, methods = [], {}, {}, []
    for model_path in model_paths:
        _, checkpoint = load_checkpoint(model_path)
        method = {"mlp": "mlp_node_only", "mlp_hop": "mlp_hop"}.get(checkpoint.get("architecture"), f"gnn_{checkpoint['variant']}")
        if "runtime" in checkpoint["groups"]:
            model_notes.append(f"{method}: trained with runtime features; evaluated WITHOUT runtime (static-only benchmark)")
        methods.append((Path(model_path), method, checkpoint.get("seed")))
    for level in RANKING_LEVELS:
        bundles = load_bundles([str(a) for a in real_apps], "diagnostic" if dry_run else "final", log, require_runtime=False,
                               label_level=level) if real_apps else []
        records = baseline_records(bundles)
        for path, method, seed in methods:
            records += model_records(bundles, path, method, seed)
        ranking[level] = summarize(records)
        ranking_samples[level] = len({(r.app, r.endpoint) for r in records})

    results = {
        "kind": "dry_run_on_dev" if dry_run else "final_on_test",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index_locked_at": index.lock.locked_at,
        "apps": [a.name for a in apps],
        "detection": {level: {"rows": d.rows(), "per_app": {
            app: {"tp": len(m.true_positives), "fp": len(m.false_positives), "fn": len(m.false_negatives)}
            for app, m in d.per_app.items()}} for level, d in detection.items()},
        "controlled_apps": [a.name for a in controlled_apps],
        "detection_controlled": {level: {"rows": d.rows(), "per_app": {
            app: {"tp": len(m.true_positives), "fp": len(m.false_positives), "fn": len(m.false_negatives)}
            for app, m in d.per_app.items()}} for level, d in detection_controlled.items()},
        # Added 2026-10-29 (after the historical final run, whose saved results do not contain these fields).
        # Diagnostics only: the official rows above are unchanged by them.
        "false_positive_breakdown": {level: fp_breakdown(d, manifests) for level, d in detection.items()},
        "sensitivity_reviewed_only": {level: reviewed_only(d, manifests) for level, d in detection.items()},
        "detection_ci95_app_bootstrap": {level: app_bootstrap({app: (len(m.true_positives), len(m.false_positives), len(m.false_negatives))
                                                               for app, m in d.per_app.items()})
                                         for level, d in detection.items()},
        "ranking_label_levels": list(RANKING_LEVELS),
        "unverified_defects_not_counted": pending,
        "pending_hits_not_scored": pending_hits,
        "ranking": ranking,
        "ranking_samples": ranking_samples,
        "model_notes": model_notes,
        "limitations": [
            "Static-only: public repositories were not executed, so runtime-based methods are unavailable.",
            "RECORDED tier = the frozen labels as drafted (AI-assisted review, not yet independently verified); "
            "SOURCE and RUNTIME tiers count only defects a second person confirmed.",
            "Detection metrics depend on the completeness of the labels; partially reviewed apps (good-c014, good-c030) undercount defects.",
            "Findings on a recorded but not-yet-verified defect are held out as 'pending' (neither TP nor FP).",
            "Small n: confidence intervals across ~20 test apps are wide.",
            "Controlled copies (team-injected defects) are scored separately, excluded from ranking, and are not real-world evidence.",
        ],
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "final_results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out / "final_report.md").write_text(render(results), encoding="utf-8")
    if not dry_run:
        log(f"Final run logged in {record_final_run(root, model_paths, [a.name for a in apps], 'scaletrace benchmark evaluate-final')}")
    return results


def _is_reviewed_non_defect(finding, manifest) -> bool:
    return any(k.rule_id == finding.rule_id and k.file == finding.file and k.line == finding.line for k in manifest.known_non_defects)


def fp_breakdown(evaluation, manifests: dict) -> dict:
    """Split false positives into hits a reviewer judged harmless (``known_non_defects``) and hits nobody has
    classified (reviewed as 'unsure', or apps that were only partially reviewed). Both stay false positives in the
    official metrics; this only shows how much of the FP count is a labelling gap rather than a reviewed error."""
    per_app = {}
    for app, match in evaluation.per_app.items():
        reviewed = sum(_is_reviewed_non_defect(f, manifests[app]) for f in match.false_positives)
        per_app[app] = {"reviewed_non_defect": reviewed, "not_reviewed": len(match.false_positives) - reviewed,
                        "same_loop_as_true_positive": same_loop_companions(match)}
    keys = ("reviewed_non_defect", "not_reviewed", "same_loop_as_true_positive")
    total = {k: sum(v[k] for v in per_app.values()) for k in keys}
    return {"total": total, "per_app": per_app,
            "note": "same_loop_as_true_positive overlaps the other two counts: false positives of a loop rule that sit in the "
                    "same loop (same rule, file, function and innermost loop) as a matched defect. ScaleTrace reports one finding "
                    "per call site, while a reviewer may record one defect per loop, so these can be a granularity "
                    "mismatch rather than distinct errors."}


def _loop_key(finding) -> tuple | None:
    if not getattr(finding, "static_evidence", None):
        return None
    loop = finding.static_evidence[0].details.get("innermost_loop")
    if not isinstance(loop, dict) or "line" not in loop:
        return None
    return finding.rule_id, finding.file, finding.function, loop["line"]


def same_loop_companions(match) -> int:
    """False positives that share the innermost loop with a true-positive finding of the same rule and function."""
    matched = {_loop_key(f) for _, f in match.true_positives} - {None}
    return sum(1 for f in match.false_positives if _loop_key(f) in matched)


def reviewed_only(evaluation, manifests: dict) -> dict:
    """SENSITIVITY ANALYSIS, not the official metric: precision counting only false positives a reviewer judged
    harmless. Its denominator (TP + reviewed FP) is smaller than the official one (TP + all FP)."""
    tp = evaluation.overall.tp
    fn = evaluation.overall.fn
    fp = fp_breakdown(evaluation, manifests)["total"]["reviewed_non_defect"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall > 0 else None
    return {"tp": tp, "fp_reviewed": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "note": "Sensitivity analysis: unreviewed hits (unsure, unclassified) are left out of the precision denominator."}


def app_bootstrap(per_app: dict[str, tuple[int, int, int]], n: int = 2000, seed: int = 0) -> dict:
    """95% percentile intervals for pooled precision, recall and F1, resampling applications with replacement.
    Applications are the independent units; findings within an app are not independent."""
    import random

    apps = sorted(per_app)
    if not apps:
        return {}
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {"precision": [], "recall": [], "f1": []}
    for _ in range(n):
        pick = [per_app[rng.choice(apps)] for _ in apps]
        tp, fp, fn = (sum(x[i] for x in pick) for i in range(3))
        p = tp / (tp + fp) if tp + fp else None
        r = tp / (tp + fn) if tp + fn else None
        for key, value in (("precision", p), ("recall", r), ("f1", 2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else None)):
            if value is not None:
                samples[key].append(value)
    out = {}
    for key, values in samples.items():
        values.sort()
        out[key] = [values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]] if values else None
    out.update({"resamples": n, "apps": len(apps), "seed": seed})
    return out


def split_pending(findings: list, manifest, level: str) -> tuple[list, list]:
    """Hold out findings that hit a recorded defect not yet verified at ``level``: they are neither TP nor FP yet."""
    open_defects = unverified_defects(manifest, level)
    held = [f for f in findings if any(f.rule_id == d.rule_id and f.file == d.file and d.line_start <= f.line <= d.line_end
                                       for d in open_defects)]
    return [f for f in findings if f not in held], held


def render(results: dict) -> str:
    title = "Dry run on the DEV split (not a final result)" if results["kind"] == "dry_run_on_dev" else "FINAL evaluation on the locked TEST split"
    lines = [f"# ScaleTrace benchmark evaluation: {title}", "",
             f"Generated {results['generated_at']}; index locked {results['index_locked_at']}; {len(results['apps'])} apps.", "",
             ]
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"
    titles = {"recorded": "Detection, RECORDED-LABELS tier (frozen labels as drafted; not independently verified)",
              "source": "Detection, SOURCE-CONFIRMED tier (pattern confirmed in code by a second person)",
              "runtime": "Detection, RUNTIME-VERIFIED tier (also confirmed at runtime, or no runtime metric)"}
    sections = [("", results["detection"])]
    if results.get("detection_controlled"):
        sections.append((f"CONTROLLED COPIES ONLY ({len(results['controlled_apps'])} apps, injected defects; not real-world evidence): ",
                         results["detection_controlled"]))
    for prefix, detection in sections:
        for level, data in detection.items():
            lines += [f"## {prefix}{titles[level]}", "", "| Level | Name | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|"]
            for row in data["rows"]:
                lines.append(f"| {row['level']} | {row['name']} | {row['tp']} | {row['fp']} | {row['fn']} | {fmt(row['precision'])} | {fmt(row['recall'])} | {fmt(row['f1'])} |")
            lines.append("")
    pending = {a: n for a, n in results["unverified_defects_not_counted"].items() if n}
    held = {level: {a: n for a, n in apps.items() if n} for level, apps in results.get("pending_hits_not_scored", {}).items()}
    lines += ["", f"Unverified defects not counted: {pending or 'none'}.",
              f"Findings on unverified defects held out (pending, neither TP nor FP): {held or 'none'}.", "",
              ]
    if results.get("false_positive_breakdown"):
        lines += ["## Diagnostics (real apps; the official rows above are unchanged)", "",
                  "| Tier | FP reviewed as non-defect | FP not reviewed | FP in the same loop as a matched defect | Reviewed-only precision (sensitivity) | 95% app-bootstrap CI: precision / recall / F1 |",
                  "|---|---|---|---|---|---|"]
        for level, breakdown in results["false_positive_breakdown"].items():
            sens = results["sensitivity_reviewed_only"][level]
            ci = results["detection_ci95_app_bootstrap"].get(level) or {}
            span = lambda k: "n/a" if not ci.get(k) else f"{ci[k][0]:.2f}-{ci[k][1]:.2f}"
            lines.append(f"| {level} | {breakdown['total']['reviewed_non_defect']} | {breakdown['total']['not_reviewed']} | "
                         f"{breakdown['total'].get('same_loop_as_true_positive', 0)} | "
                         f"{fmt(sens['precision'])} | {span('precision')} / {span('recall')} / {span('f1')} |")
        lines += ["", "A tier with no positives (no verified defects) has precision 0 by definition and no recall; "
                  "read it as 'nothing is verified yet', not as a measured precision.", ""]
    for level in results["ranking_label_levels"]:
        lines += [f"## Root-cause ranking, {level.upper()} labels ({results['ranking_samples'][level]} symptoms, static-only)", "",
                  "| Method | n | Top-1 | Top-3 | MRR |", "|---|---|---|---|---|"]
        for method, data in sorted(results["ranking"][level].items()):
            s = data["overall"]
            if s.get("n"):
                lines.append(f"| {method} | {s['n']} | {s['top1']:.2f} | {s['top3']:.2f} | {s['mrr']:.2f} |")
        lines.append("")
    lines += ["## Notes and limitations", ""] + [f"- {n}" for n in dict.fromkeys(results["model_notes"] + results["limitations"])]
    return "\n".join(lines) + "\n"
