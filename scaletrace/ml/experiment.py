"""Leakage-resistant root-cause ranking experiment with a pre-declared validation gate.

Splits (all synthetic; benchmark apps are refused by the guard):

* ``in_distribution``     held-out generator seeds, same style as training (reference only)
* ``style_holdout``       a code style never seen in training                          (gate split)
* ``leave_one_kind_out``  for each defect kind K: models trained without any K samples, evaluated on
                          the K samples of the held-out sets; results pooled over K   (gate split)

Methods: random, proximity, rules_only, heuristic_static, heuristic_combined (non-learned), the
node-only MLP (learned, no graph), and the GNN variants. The PRIMARY model is ``gnn_combined``
(static + runtime + code); the other GNN variants are ablations.

Pre-declared validation gate (fixed before running; see docs/ml.md):

* C1  the shortcut suite verdict is PASS (ml/shortcuts.py);
* C2  on every gate split, the lower bound of the primary GNN's 95% app-level bootstrap CI of MRR is
      above the upper bound of the best non-learned baseline's CI AND of the node-only MLP's CI;
* C3  on every gate split, every training seed of the primary GNN individually has a higher MRR than
      the best non-learned baseline and than the node-only MLP (mean over seeds).

Only if C1-C3 all pass is the GNN reported as validated -- on synthetic held-out data. A claim about
real applications additionally requires the locked benchmark test split.
"""

from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scaletrace.diagnosis import DiagnosisConfig
from scaletrace.ml.dataset import AppBundle, build_samples, find_apps, load_bundle
from scaletrace.ml.evaluate import RankRecord, gnn_records, heuristic_records
from scaletrace.ml.features import VARIANT_GROUPS, FeatureBuilder
from scaletrace.ml.shortcuts import (
    ShortcutReport,
    fingerprint_check,
    make_renamer,
    near_duplicate_check,
    node_only_check,
    rename_check,
    saturation_check,
    single_feature_check,
)
from scaletrace.ml.train import TrainConfig, load_checkpoint, save_checkpoint, train_model

PRIMARY = "gnn_combined"
NODE_ONLY = "mlp_node_only"
NEIGHBOR_MLP = "mlp_hop"
NON_LEARNED = ["random", "proximity", "rules_only", "heuristic_static", "heuristic_combined"]
# Added in Phase 2 alongside (not replacing) the pre-declared gate: stronger baselines that also see
# edge evidence. Reported as a separate "strict comparison"; they do not change C1-C3.
EXTRA_BASELINES = ["heuristic_combined_edges", NEIGHBOR_MLP]
GATE_SPLITS = ["style_holdout", "leave_one_kind_out"]
DEFAULT_VARIANTS = ["static", "combined", "combined_no_rules", "runtime_only", "static_no_code", "combined_no_code"]


@dataclass
class ExperimentConfig:
    train_roots: list[str]
    style_roots: list[str]
    indist_roots: list[str] = field(default_factory=list)
    extra_roots: list[list[str]] = field(default_factory=list)
    variants: list[str] = field(default_factory=lambda: list(DEFAULT_VARIANTS))
    seeds: int = 3
    epochs: int = 150
    embedder: str = "hashed"
    code_dim: int = 32
    loko: bool = True
    bootstrap: int = 2000
    val_fraction: float = 0.2


def make_builder(groups: list[str], embedder: str, code_dim: int, source_transform=None) -> FeatureBuilder:
    from scaletrace.ml.embeddings import make_embedder

    return FeatureBuilder(groups, make_embedder(embedder, code_dim) if "code" in groups else None, code_dim, source_transform)


def load_bundles(roots: list[str], access: str, log=print, require_runtime: bool = True, label_level: str = "runtime") -> list[AppBundle]:
    apps = find_apps(roots, access=access)
    bundles = [load_bundle(app, require_verified=True, label_level=label_level) for app in apps]
    usable = [b for b in bundles if b.runtime is not None or not require_runtime]
    rejected = sum(b.labels.get("rejected_symptoms", 0) for b in usable)
    labels = sum(len(b.labels.get("symptoms", [])) for b in usable)
    log(f"Loaded {len(usable)}/{len(bundles)} apps from {', '.join(roots)}: {labels} verified labels"
        f" ({rejected} rejected by runtime verification){'; apps without runtime skipped' if len(usable) < len(bundles) else ''}")
    return usable


# --- training -------------------------------------------------------------------------------------

def train_one(bundles: list[AppBundle], variant: str, architecture: str, seed: int, cfg: ExperimentConfig, out: Path,
              exclude_kinds: set[str] | None = None, log=print) -> tuple[Path, tuple[str, int, float]]:
    groups = VARIANT_GROUPS[variant]
    builder = make_builder(groups, cfg.embedder, cfg.code_dim)
    use_runtime = "runtime" in groups
    apps = sorted(bundles, key=lambda b: b.name)
    random.Random(seed).shuffle(apps)
    n_val = max(1, int(len(apps) * cfg.val_fraction)) if len(apps) > 4 else 0
    val_apps, train_apps = apps[:n_val], apps[n_val:]

    def samples(bs):
        return [s for b in bs for s in build_samples(b, builder, use_runtime) if not exclude_kinds or s.defect not in exclude_kinds]

    train, validation = samples(train_apps), samples(val_apps)
    name = {"mlp": NODE_ONLY, "mlp_hop": NEIGHBOR_MLP}.get(architecture, f"gnn_{variant}")
    result = train_model(train, validation, builder.names, TrainConfig(epochs=cfg.epochs, seed=seed, architecture=architecture), log=lambda *_: None)
    best = max(h["validation_mrr"] for h in result.history)
    log(f"  {name:<24} seed {seed}{' (without ' + ','.join(sorted(exclude_kinds)) + ')' if exclude_kinds else ''}: "
        f"{len(train)} train / {len(validation)} val samples, best epoch {result.best_epoch}, val MRR {best:.3f}")
    path = save_checkpoint(out, result, {
        "variant": variant, "groups": groups, "embedder": cfg.embedder, "code_dim": cfg.code_dim,
        "feature_names": builder.names, "train_apps": [b.name for b in train_apps], "seed": seed,
        "excluded_kinds": sorted(exclude_kinds or []),
    })
    return path, (f"{name}/seed{seed}", result.best_epoch, best)


def model_records(bundles: list[AppBundle], path: Path, method: str, seed: int, kinds: set[str] | None = None, source_transform=None) -> list[RankRecord]:
    model, checkpoint = load_checkpoint(path)
    builder = make_builder(checkpoint["groups"], checkpoint["embedder"], checkpoint["code_dim"], source_transform)
    records = []
    for bundle in bundles:
        records += gnn_records(method, model, checkpoint, builder, bundle, seed=seed)
    return [r for r in records if kinds is None or r.defect in kinds]


def baseline_records(bundles: list[AppBundle], kinds: set[str] | None = None) -> list[RankRecord]:
    config = DiagnosisConfig()
    records = [r for b in bundles for r in heuristic_records(b, config)]
    return [r for r in records if kinds is None or r.defect in kinds]


# --- statistics -----------------------------------------------------------------------------------

def _metrics(records: list[RankRecord]) -> dict:
    n = len(records)
    if not n:
        return {"n": 0, "top1": None, "top3": None, "mrr": None}
    return {
        "n": n,
        "top1": sum(1 for r in records if r.rank is not None and r.rank <= 1.0) / n,
        "top3": sum(1 for r in records if r.rank is not None and r.rank <= 3.0) / n,
        "mrr": sum(r.reciprocal for r in records) / n,
    }


def method_stats(records: list[RankRecord], method: str, n_boot: int, seed: int = 0) -> dict:
    mine = [r for r in records if r.method == method]
    by_seed: dict = defaultdict(list)
    for r in mine:
        by_seed[r.seed].append(r)
    per_seed = {str(s): _metrics(rs) for s, rs in sorted(by_seed.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))}
    mrrs = [m["mrr"] for m in per_seed.values() if m["mrr"] is not None]
    top1s = [m["top1"] for m in per_seed.values() if m["top1"] is not None]

    # Seed-averaged reciprocal rank per sample, grouped by app, then an app-level bootstrap.
    per_sample: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in mine:
        per_sample[(r.app, r.endpoint)].append(r.reciprocal)
    by_app: dict[str, list[float]] = defaultdict(list)
    for (app, _), values in per_sample.items():
        by_app[app].append(sum(values) / len(values))
    apps = sorted(by_app)
    point = sum(sum(v) for v in by_app.values()) / max(1, sum(len(v) for v in by_app.values()))
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot if apps else 0):
        drawn = [by_app[rng.choice(apps)] for _ in apps]
        total = sum(len(v) for v in drawn)
        boots.append(sum(sum(v) for v in drawn) / total if total else 0.0)
    boots.sort()
    ci = (boots[int(0.025 * len(boots))], boots[min(len(boots) - 1, int(0.975 * len(boots)))]) if boots else (None, None)
    return {
        "n_samples": len(per_sample),
        "n_apps": len(apps),
        "per_seed": per_seed,
        "mrr_mean": statistics.fmean(mrrs) if mrrs else None,
        "mrr_sd": statistics.stdev(mrrs) if len(mrrs) > 1 else 0.0 if mrrs else None,
        "top1_mean": statistics.fmean(top1s) if top1s else None,
        "top1_sd": statistics.stdev(top1s) if len(top1s) > 1 else 0.0 if top1s else None,
        "mrr_pooled": point,
        "ci95": list(ci),
    }


def split_summary(records: list[RankRecord], n_boot: int, graph_required: dict | None = None) -> dict:
    methods = sorted({r.method for r in records})
    summary = {m: method_stats(records, m, n_boot) for m in methods}
    subsets = {}
    if graph_required:
        for subset, wanted in (("graph_required", True), ("other", False)):
            chosen = [r for r in records if graph_required.get((r.app, r.endpoint), False) == wanted]
            if chosen:
                subsets[subset] = {m: method_stats(chosen, m, n_boot) for m in methods if any(r.method == m for r in chosen)}
    by_kind: dict[str, dict] = defaultdict(dict)
    for kind in sorted({r.defect for r in records}):
        for m in methods:
            subset = [r for r in records if r.method == m and r.defect == kind]
            by_kind[kind][m] = _metrics(subset)["top1"] if subset else None
    return {"methods": summary, "top1_by_kind": dict(by_kind), "subsets": subsets}


# --- gate ----------------------------------------------------------------------------------------

def evaluate_gate(splits: dict[str, dict], shortcuts: ShortcutReport) -> dict:
    criteria = [{"id": "C1", "description": "shortcut suite PASS", "passed": shortcuts.verdict == "PASS",
                 "detail": f"verdict {shortcuts.verdict}"}]
    for split in GATE_SPLITS:
        methods = splits.get(split, {}).get("methods", {})
        primary, mlp = methods.get(PRIMARY), methods.get(NODE_ONLY)
        baselines = {m: methods[m] for m in NON_LEARNED if m in methods and methods[m]["mrr_pooled"] is not None}
        if primary is None or mlp is None or not baselines:
            for cid in ("C2", "C3"):
                criteria.append({"id": f"{cid}/{split}", "description": "data missing", "passed": False, "detail": "split not evaluated"})
            continue
        best_name, best = max(baselines.items(), key=lambda kv: kv[1]["mrr_pooled"])
        lo = primary["ci95"][0]
        c2 = lo is not None and lo > best["ci95"][1] and lo > mlp["ci95"][1]
        criteria.append({
            "id": f"C2/{split}", "passed": bool(c2),
            "description": "primary GNN CI above best non-learned baseline CI and node-only MLP CI",
            "detail": f"{PRIMARY} CI {_fmt_ci(primary['ci95'])}; best baseline {best_name} CI {_fmt_ci(best['ci95'])}; {NODE_ONLY} CI {_fmt_ci(mlp['ci95'])}",
        })
        seed_mrrs = [m["mrr"] for m in primary["per_seed"].values() if m["mrr"] is not None]
        c3 = bool(seed_mrrs) and all(v > best["mrr_pooled"] and v > (mlp["mrr_mean"] or 0.0) for v in seed_mrrs)
        criteria.append({
            "id": f"C3/{split}", "passed": c3,
            "description": "every primary GNN seed beats the best non-learned baseline and the node-only MLP",
            "detail": f"seed MRRs {[round(v, 3) for v in seed_mrrs]} vs {best_name} {best['mrr_pooled']:.3f}, {NODE_ONLY} {mlp['mrr_mean'] or 0:.3f}",
        })
    validated = all(c["passed"] for c in criteria)
    strict = []
    for split in GATE_SPLITS:
        methods = splits.get(split, {}).get("methods", {})
        primary = methods.get(PRIMARY)
        for extra in EXTRA_BASELINES:
            other = methods.get(extra)
            if primary and other and primary["ci95"][0] is not None and other["ci95"][1] is not None:
                strict.append({"split": split, "baseline": extra, "gnn_ci": primary["ci95"], "baseline_ci": other["ci95"],
                               "gnn_above": primary["ci95"][0] > other["ci95"][1]})
    return {
        "strict_comparison": strict,
        "criteria": criteria,
        "validated": validated,
        "verdict": ("GNN VALIDATED on synthetic held-out data (a real-world claim still requires the locked benchmark test split)"
                    if validated else "GNN NOT VALIDATED: at least one pre-declared criterion failed"),
    }


def _fmt_ci(ci) -> str:
    return "n/a" if ci[0] is None else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


# --- shortcut suite -------------------------------------------------------------------------------

def shortcut_suite(train: list[AppBundle], test: list[AppBundle], cfg: ExperimentConfig, mlp_records: list[RankRecord],
                   primary_paths: list[Path], histories: list[tuple[str, int, float]], log=print) -> ShortcutReport:
    builder = make_builder(VARIANT_GROUPS["combined"], cfg.embedder, cfg.code_dim)
    train_samples = [s for b in train for s in build_samples(b, builder, True)]
    test_samples = [s for b in test for s in build_samples(b, builder, True)]
    report = ShortcutReport()
    report.checks.append(single_feature_check(train_samples, builder.names))
    mlp = method_stats(mlp_records, NODE_ONLY, 0) if mlp_records else None
    report.checks.append(node_only_check(mlp["top1_mean"] if mlp else None, "node-only MLP mean Top-1 on the held-out style"))
    report.checks.append(fingerprint_check(train_samples, test_samples, builder.names))
    if primary_paths:
        base = method_stats(model_records(test, primary_paths[0], PRIMARY, 0), PRIMARY, 0)["mrr_pooled"]
        renamed = method_stats(model_records(test, primary_paths[0], PRIMARY, 0, source_transform=make_renamer(17)), PRIMARY, 0)["mrr_pooled"]
        report.checks.append(rename_check(base, renamed))
    else:
        report.checks.append(rename_check(None, None))
    report.checks.append(saturation_check(histories))
    report.checks.append(near_duplicate_check([b.path for b in train], [b.path for b in test]))
    log(report.render())
    return report


# --- orchestration --------------------------------------------------------------------------------

def run_experiment(cfg: ExperimentConfig, out: Path, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    models = out / "models"
    train = load_bundles(cfg.train_roots, "training", log)
    style = load_bundles(cfg.style_roots, "diagnostic", log)
    indist = load_bundles(cfg.indist_roots, "diagnostic", log) if cfg.indist_roots else []
    extras = [(" + ".join(r), load_bundles(r, "diagnostic", log, require_runtime=False)) for r in cfg.extra_roots]

    log("Training (model selection = early stopping on held-out *synthetic* training apps only)")
    checkpoints: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    histories = []
    for variant in cfg.variants:
        for seed in range(cfg.seeds):
            path, history = train_one(train, variant, "gnn", seed, cfg, models / f"gnn_{variant}_s{seed}.pt", log=log)
            checkpoints[f"gnn_{variant}"].append((seed, path))
            histories.append(history)
    for seed in range(cfg.seeds):
        for architecture, method in (("mlp", NODE_ONLY), ("mlp_hop", NEIGHBOR_MLP)):
            path, history = train_one(train, "combined", architecture, seed, cfg, models / f"{method}_s{seed}.pt", log=log)
            checkpoints[method].append((seed, path))
            histories.append(history)

    graph_required = {
        (b.name, s["endpoint"]): bool(s.get("graph_required"))
        for b in train + style + indist for s in b.labels.get("symptoms", [])
    }

    def evaluate_set(bundles: list[AppBundle]) -> list[RankRecord]:
        records = baseline_records(bundles)
        for method, paths in checkpoints.items():
            for seed, path in paths:
                records += model_records(bundles, path, method, seed)
        return records

    splits: dict[str, dict] = {}
    style_records = evaluate_set(style)
    splits["style_holdout"] = split_summary(style_records, cfg.bootstrap, graph_required)
    if indist:
        splits["in_distribution"] = split_summary(evaluate_set(indist), cfg.bootstrap, graph_required)
    for name, bundles in extras:
        splits[f"extra: {name}"] = split_summary(evaluate_set(bundles), cfg.bootstrap)

    if cfg.loko:
        log("Leave-one-kind-out (primary GNN and node-only MLP retrained without each kind)")
        held = style + indist
        kinds = sorted({s.get("defect") for b in train for s in b.labels.get("symptoms", [])})
        loko_records: list[RankRecord] = []
        for kind in kinds:
            loko_records += baseline_records(held, {kind})
            for seed in range(cfg.seeds):
                for architecture, method in (("gnn", PRIMARY), ("mlp", NODE_ONLY), ("mlp_hop", NEIGHBOR_MLP)):
                    path, _ = train_one(train, "combined", architecture, seed, cfg, models / f"loko_{kind}_{method}_s{seed}.pt", {kind}, log)
                    loko_records += model_records(held, path, method, seed, {kind})
        splits["leave_one_kind_out"] = split_summary(loko_records, cfg.bootstrap, graph_required)

    shortcuts = shortcut_suite(train, style, cfg, [r for r in style_records if r.method == NODE_ONLY],
                               [p for _, p in checkpoints[PRIMARY]], histories, log)
    gate = evaluate_gate(splits, shortcuts)
    results = {
        "config": {**cfg.__dict__},
        "primary_model": PRIMARY,
        "splits": splits,
        "shortcut_suite": shortcuts.to_dict(),
        "gate": gate,
        "training_runs": [{"run": n, "best_epoch": e, "val_mrr": v} for n, e, v in histories],
    }
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out / "results.md").write_text(render_results(results), encoding="utf-8")
    log(render_results(results))
    return results


def render_results(results: dict) -> str:
    cfg = results["config"]
    lines = [
        "# ScaleTrace learned root-cause ranking experiment", "",
        f"Training: {', '.join(cfg['train_roots'])}; seeds {cfg['seeds']}; embedder {cfg['embedder']}; "
        f"primary model `{results['primary_model']}`. All data synthetic; benchmark apps were refused by the guard.", "",
        "These numbers come from this run only. They are not claims about real applications.", "",
        f"## Verdict: {results['gate']['verdict']}", "",
        "| Criterion | Passed | Detail |", "|---|---|---|",
    ]
    for c in results["gate"]["criteria"]:
        lines.append(f"| {c['id']} {c['description']} | {'yes' if c['passed'] else '**no**'} | {c['detail']} |")
    strict = results["gate"].get("strict_comparison", [])
    if strict:
        lines += ["", "Strict comparison (Phase 2 extra baselines; informational, not part of C1-C3):", "",
                  "| Split | Baseline | GNN CI | Baseline CI | GNN CI entirely above |", "|---|---|---|---|---|"]
        lines += [f"| {r['split']} | {r['baseline']} | {_fmt_ci(r['gnn_ci'])} | {_fmt_ci(r['baseline_ci'])} | {'yes' if r['gnn_above'] else 'no'} |" for r in strict]
    shortcuts = results["shortcut_suite"]
    lines += ["", f"## Shortcut suite: {shortcuts['verdict']}", "", "| Check | Status | Value | Threshold | Detail |", "|---|---|---|---|---|"]
    for c in shortcuts["checks"]:
        value = "n/a" if c["value"] is None else f"{c['value']:.3f}"
        threshold = "n/a" if c["threshold"] is None else f"{c['threshold']:.2f}"
        lines.append(f"| {c['name']} | {c['status'].upper()} | {value} | {threshold} | {c['detail']} |")
    order = NON_LEARNED + ["heuristic_combined_edges", NODE_ONLY, NEIGHBOR_MLP]
    for split, data in results["splits"].items():
        methods = data["methods"]
        names = [m for m in order if m in methods] + sorted(m for m in methods if m not in order)
        lines += ["", f"## Split: {split}", "",
                  "| Method | samples | apps | MRR mean ± sd (seeds) | MRR 95% CI (app bootstrap) | Top-1 mean ± sd |", "|---|---|---|---|---|---|"]
        for m in names:
            s = methods[m]
            mrr = "n/a" if s["mrr_mean"] is None else f"{s['mrr_mean']:.3f} ± {s['mrr_sd']:.3f}"
            top1 = "n/a" if s["top1_mean"] is None else f"{s['top1_mean']:.3f} ± {s['top1_sd']:.3f}"
            lines.append(f"| {m} | {s['n_samples']} | {s['n_apps']} | {mrr} | {_fmt_ci(s['ci95'])} | {top1} |")
        for subset, stats in data.get("subsets", {}).items():
            lines += ["", f"Subset `{subset}` ({next(iter(stats.values()))['n_samples']} samples):", "",
                      "| Method | MRR mean ± sd | MRR 95% CI | Top-1 mean |", "|---|---|---|---|"]
            for m in [m for m in names if m in stats]:
                st = stats[m]
                mrr = "n/a" if st["mrr_mean"] is None else f"{st['mrr_mean']:.3f} ± {st['mrr_sd']:.3f}"
                top1 = "n/a" if st["top1_mean"] is None else f"{st['top1_mean']:.3f}"
                lines.append(f"| {m} | {mrr} | {_fmt_ci(st['ci95'])} | {top1} |")
        kinds = data["top1_by_kind"]
        if kinds:
            lines += ["", "Top-1 by defect kind (pooled over seeds):", "", "| Method | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
            for m in names:
                cells = ["-" if kinds[k].get(m) is None else f"{kinds[k][m]:.2f}" for k in kinds]
                lines.append(f"| {m} | " + " | ".join(cells) + " |")
    lines += ["", "## Training runs", "", "| Run | Best epoch | Validation MRR |", "|---|---|---|"]
    lines += [f"| {r['run']} | {r['best_epoch']} | {r['val_mrr']:.3f} |" for r in results["training_runs"]]
    return "\n".join(lines) + "\n"
