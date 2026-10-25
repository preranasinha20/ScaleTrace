"""`scaletrace ml ...`: generate, collect-runtime, verify-labels, audit-data, train, evaluate, experiment.

Benchmark protection: every command that trains or selects models loads apps with access level
"training" (benchmark apps refused); evaluation uses "diagnostic" (test split refused) unless
`--final` is given, which needs a locked index and is logged (see scaletrace/benchmark/guard.py).
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2


def add_ml_parser(sub) -> None:
    ml = sub.add_parser("ml", help="Learned root-cause ranking: synthetic data, GNN training and evaluation.")
    ml_sub = ml.add_subparsers(dest="ml_command", required=True)

    gen = ml_sub.add_parser("generate", help="Generate synthetic FastAPI apps with labelled root causes (generator v4).")
    gen.add_argument("out")
    gen.add_argument("--n", type=int, default=40, help="Number of apps.")
    gen.add_argument("--style", choices=["A", "B"], default="A")
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--defects", type=int, default=4, help="Defect endpoints per app.")
    gen.add_argument("--clean", type=int, default=2, help="Clean/decoy-only endpoints per app.")
    gen.set_defaults(ml_handler=_cmd_generate)

    collect = ml_sub.add_parser("collect-runtime", help="Run each generated app's workload (EXECUTES the apps).")
    collect.add_argument("roots", nargs="+")
    collect.add_argument("--allow-execute", action="store_true")
    collect.add_argument("--workers", type=int, default=4)
    collect.add_argument("--force", action="store_true", help="Re-run apps that already have runtime data.")
    collect.set_defaults(ml_handler=_cmd_collect)

    verify = ml_sub.add_parser("verify-labels", help="Mark generated labels runtime-verified/rejected from cached runtime data.")
    verify.add_argument("roots", nargs="+")
    verify.set_defaults(ml_handler=_cmd_verify)

    audit = ml_sub.add_parser("audit-data", help="Run the shortcut-check suite (trains the node-only MLP) on a train/test pair.")
    audit.add_argument("--train", nargs="+", required=True)
    audit.add_argument("--test", nargs="+", required=True)
    audit.add_argument("--seeds", type=int, default=3)
    audit.add_argument("--epochs", type=int, default=150)
    audit.add_argument("--json", metavar="FILE")
    audit.set_defaults(ml_handler=_cmd_audit)

    train = ml_sub.add_parser("train", help="Train one model on generated apps.")
    train.add_argument("roots", nargs="+")
    train.add_argument("--out", required=True, help="Checkpoint path (.pt).")
    train.add_argument("--variant", default="combined", help="static | combined | combined_no_rules | runtime_only | static_no_code | combined_no_code")
    train.add_argument("--architecture", choices=["gnn", "mlp"], default="gnn")
    train.add_argument("--embedder", default="hashed", choices=["hashed", "codebert"])
    train.add_argument("--epochs", type=int, default=150)
    train.add_argument("--seed", type=int, default=0)
    train.set_defaults(ml_handler=_cmd_train)

    evaluate = ml_sub.add_parser("evaluate", help="Compare heuristic and learned rankers on labelled apps.")
    evaluate.add_argument("roots", nargs="+")
    evaluate.add_argument("--models", nargs="*", default=[], help="Checkpoints to evaluate.")
    evaluate.add_argument("--final", action="store_true", help="Final evaluation: may read the LOCKED test split; logged.")
    evaluate.add_argument("--json", metavar="FILE")
    evaluate.add_argument("--markdown", metavar="FILE")
    evaluate.set_defaults(ml_handler=_cmd_evaluate)

    experiment = ml_sub.add_parser("experiment", help="Full leakage-resistant experiment with the pre-declared validation gate.")
    experiment.add_argument("--train", nargs="+", required=True)
    experiment.add_argument("--style-test", nargs="+", required=True, help="Held-out code style (gate split).")
    experiment.add_argument("--indist-test", nargs="+", default=[], help="Held-out seeds of the training style.")
    experiment.add_argument("--extra-test", nargs="+", action="append", default=[], help="Additional diagnostic sets (repeatable).")
    experiment.add_argument("--out", required=True)
    experiment.add_argument("--variants", nargs="+")
    experiment.add_argument("--embedder", default="hashed", choices=["hashed", "codebert"])
    experiment.add_argument("--epochs", type=int, default=150)
    experiment.add_argument("--seeds", type=int, default=3)
    experiment.add_argument("--no-loko", action="store_true", help="Skip leave-one-kind-out (the gate then fails).")
    experiment.add_argument("--bootstrap", type=int, default=2000)
    experiment.set_defaults(ml_handler=_cmd_experiment)


def _cmd_generate(args) -> int:
    from scaletrace.ml.generator import AppGenerator

    out = Path(args.out)
    total = 0
    for i in range(args.n):
        labels = AppGenerator(seed=args.seed + i, style=args.style).generate(out / f"app_{args.style}_{args.seed + i:04d}", args.defects, args.clean)
        total += len(labels["symptoms"])
    print(f"Generated {args.n} apps (style {args.style}) with {total} labelled symptoms in {out}")
    return EXIT_OK


def _collect_one(app: Path, force: bool) -> str:
    from scaletrace.ml.dataset import RUNTIME_CACHE
    from scaletrace.runtime import WORKLOAD_FILE, load_workload
    from scaletrace.runtime.runner import run_workload

    target = app / RUNTIME_CACHE
    if target.exists() and not force:
        return f"{app.name}: cached"
    for stale in app.glob("*.db"):
        stale.unlink()
    report = run_workload(app, load_workload(app / WORKLOAD_FILE), target.parent)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return f"{app.name}: {report.status} in {report.duration_s}s" + (f" ({report.error.splitlines()[0]})" if report.error else "")


def _cmd_collect(args) -> int:
    from scaletrace.ml.dataset import find_apps

    if not args.allow_execute:
        print("error: collect-runtime executes every app; re-run with --allow-execute.", file=sys.stderr)
        return EXIT_USAGE
    apps = find_apps(args.roots, access="training")  # refuses benchmark apps: Phase 1 never executes them
    print(f"Collecting runtime evidence for {len(apps)} apps with {args.workers} workers (this EXECUTES the apps) ...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for line in pool.map(lambda a: _collect_one(a, args.force), apps):
            print(" ", line)
    return EXIT_OK


def _cmd_verify(args) -> int:
    from scaletrace.ml.dataset import find_apps
    from scaletrace.ml.verify import verify_app

    from collections import Counter

    totals: Counter = Counter()
    for app in find_apps(args.roots, access="training"):
        totals.update(verify_app(app))
    print(f"Label verification: {dict(totals)}")
    return EXIT_OK


def _cmd_audit(args) -> int:
    from scaletrace.ml.experiment import ExperimentConfig, NODE_ONLY, load_bundles, model_records, shortcut_suite, train_one

    cfg = ExperimentConfig(train_roots=args.train, style_roots=args.test, seeds=args.seeds, epochs=args.epochs, loko=False)
    train = load_bundles(args.train, "training")
    test = load_bundles(args.test, "diagnostic")
    out = Path(args.json).parent if args.json else Path("scaletrace_output") / "audit"
    records, histories = [], []
    for seed in range(args.seeds):
        path, history = train_one(train, "combined", "mlp", seed, cfg, out / f"audit_mlp_s{seed}.pt")
        records += model_records(test, path, NODE_ONLY, seed)
        histories.append(history)
    report = shortcut_suite(train, test, cfg, records, [], histories)
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return EXIT_OK


def _cmd_train(args) -> int:
    from scaletrace.ml.experiment import ExperimentConfig, load_bundles, train_one
    from scaletrace.ml.features import VARIANT_GROUPS

    if args.variant not in VARIANT_GROUPS:
        print(f"error: unknown variant {args.variant}; choose from {sorted(VARIANT_GROUPS)}", file=sys.stderr)
        return EXIT_USAGE
    cfg = ExperimentConfig(train_roots=args.roots, style_roots=[], embedder=args.embedder, epochs=args.epochs)
    path, _ = train_one(load_bundles(args.roots, "training"), args.variant, args.architecture, args.seed, cfg, Path(args.out))
    print(f"Checkpoint written to {path}")
    return EXIT_OK


def _cmd_evaluate(args) -> int:
    from scaletrace.benchmark.guard import classify, record_final_run
    from scaletrace.ml.evaluate import render_markdown, summarize
    from scaletrace.ml.experiment import baseline_records, load_bundles, model_records
    from scaletrace.ml.train import load_checkpoint

    bundles = load_bundles(args.roots, "final" if args.final else "diagnostic", require_runtime=False)
    records = baseline_records(bundles)
    for model_path in args.models:
        _, checkpoint = load_checkpoint(model_path)
        method = "mlp_node_only" if checkpoint.get("architecture") == "mlp" else f"gnn_{checkpoint['variant']}"
        records += model_records(bundles, Path(model_path), method, checkpoint.get("seed"))
    if args.final:
        roots = {loc[0] for loc in (classify(b.path)[2] for b in bundles) if loc is not None}
        for root in sorted(roots):
            print(f"Final evaluation logged in {record_final_run(root, args.models, [b.name for b in bundles], 'scaletrace ml evaluate --final')}")
    summary = summarize(records)
    markdown = render_markdown(summary, f"Root-cause ranking on {', '.join(args.roots)}")
    print(markdown)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    return EXIT_OK


def _cmd_experiment(args) -> int:
    from scaletrace.ml.experiment import DEFAULT_VARIANTS, ExperimentConfig, run_experiment

    cfg = ExperimentConfig(
        train_roots=args.train, style_roots=args.style_test, indist_roots=args.indist_test, extra_roots=args.extra_test,
        variants=args.variants or list(DEFAULT_VARIANTS), seeds=args.seeds, epochs=args.epochs, embedder=args.embedder,
        loko=not args.no_loko, bootstrap=args.bootstrap,
    )
    out = Path(args.out)
    log_lines: list[str] = []

    def log(line: str) -> None:
        print(line, flush=True)
        log_lines.append(line)

    run_experiment(cfg, out, log)
    (out / "experiment_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    print(f"Results written to {out / 'results.md'} and {out / 'results.json'}")
    return EXIT_OK
