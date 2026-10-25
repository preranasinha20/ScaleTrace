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
from scaletrace.benchmark.schema import VERIFICATION_LEVELS
from scaletrace.benchmark.validate import validate_benchmark
from scaletrace.rules.rulebook import Rulebook


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
    detection_inputs: dict[str, dict[str, dict]] = {subset: {level: {} for level in VERIFICATION_LEVELS} for subset in ("real", "controlled")}
    pending, real_apps, controlled_apps = {}, [], []
    for app in apps:
        manifest = load_manifest(app)
        subset = "controlled" if manifest.origin.controlled else "real"
        (controlled_apps if manifest.origin.controlled else real_apps).append(app)
        static = run_static_analysis(app).report
        for level in VERIFICATION_LEVELS:
            detection_inputs[subset][level][manifest.app_id] = (static.findings, groundtruth_from_manifest(manifest, static.rules_applied, level))
        pending[manifest.app_id] = len(unverified_defects(manifest, "source"))
    detection = {level: evaluate(detection_inputs["real"][level], rulebook) for level in VERIFICATION_LEVELS}
    detection_controlled = {level: evaluate(detection_inputs["controlled"][level], rulebook)
                            for level in VERIFICATION_LEVELS} if controlled_apps else {}

    # Static-only benchmark: ranking uses source-confirmed defects (runtime verification needs the sandbox).
    bundles = load_bundles([str(a) for a in real_apps], "diagnostic" if dry_run else "final", log, require_runtime=False, label_level="source") \
        if real_apps else []
    records = baseline_records(bundles)
    model_notes = []
    for model_path in model_paths:
        _, checkpoint = load_checkpoint(model_path)
        method = {"mlp": "mlp_node_only", "mlp_hop": "mlp_hop"}.get(checkpoint.get("architecture"), f"gnn_{checkpoint['variant']}")
        if "runtime" in checkpoint["groups"]:
            model_notes.append(f"{method}: trained with runtime features; evaluated WITHOUT runtime (static-only benchmark)")
        records += model_records(bundles, Path(model_path), method, checkpoint.get("seed"))
    ranking = summarize(records)

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
        "ranking_label_level": "source",
        "unverified_defects_not_counted": pending,
        "ranking": ranking,
        "ranking_samples": len({(r.app, r.endpoint) for r in records}),
        "model_notes": model_notes,
        "limitations": [
            "Static-only: public repositories were not executed, so runtime-based methods are unavailable.",
            "Only fully verified defects count; detection metrics depend on the completeness of human labels.",
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


def render(results: dict) -> str:
    title = "Dry run on the DEV split (not a final result)" if results["kind"] == "dry_run_on_dev" else "FINAL evaluation on the locked TEST split"
    lines = [f"# ScaleTrace benchmark evaluation: {title}", "",
             f"Generated {results['generated_at']}; index locked {results['index_locked_at']}; {len(results['apps'])} apps.", "",
             ]
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"
    titles = {"source": "Detection, SOURCE-CONFIRMED tier (pattern confirmed in code by a person)",
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
    lines += ["", f"Unverified defects not counted: {pending or 'none'}.", "",
              f"## Root-cause ranking ({results['ranking_samples']} source-confirmed symptoms, static-only)", "",
              "| Method | n | Top-1 | Top-3 | MRR |", "|---|---|---|---|---|"]
    for method, data in sorted(results["ranking"].items()):
        s = data["overall"]
        if s.get("n"):
            lines.append(f"| {method} | {s['n']} | {s['top1']:.2f} | {s['top3']:.2f} | {s['mrr']:.2f} |")
    lines += ["", "## Notes and limitations", ""] + [f"- {n}" for n in results["model_notes"] + results["limitations"]]
    return "\n".join(lines) + "\n"
