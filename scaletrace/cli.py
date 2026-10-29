"""Command-line interface.

    scaletrace analyze  PATH [--json OUT] [--rules ...]        static analysis (never executes code)
    scaletrace graph    PATH [--out DIR]                       knowledge graph (JSON + interactive HTML)
    scaletrace run      PATH --allow-execute [--workload F]    controlled runtime test (EXECUTES target code)
    scaletrace diagnose PATH [--runtime F | --run --allow-execute] [--endpoint EP] [--model M]
    scaletrace evaluate PATH... [--csv OUT]                     precision/recall/F1 against ground truth
    scaletrace ml ...                                           learned root-cause ranking (see `scaletrace ml -h`)
    scaletrace rules | scaletrace schema
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from scaletrace import __version__
from scaletrace.analyzer.ingestion import RepositoryError
from scaletrace.pipeline import UnknownRuleError, run_static_analysis
from scaletrace.reports import render_text, report_json_schema, write_json_report
from scaletrace.rules import ConfigError, RulebookError, load_config, load_rulebook
from scaletrace.rules.detectors import DETECTORS

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME_FAILED = 3
DEFAULT_OUT = "scaletrace_output"

EXECUTION_WARNING = (
    "WARNING: runtime testing EXECUTES code from {path} in a local subprocess bound to 127.0.0.1.\n"
    "Only run repositories you trust, preferably inside an isolated environment or container."
)


def _add_static_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rules", help="Comma-separated rule IDs to apply (default: all implemented).")
    parser.add_argument("--rulebook", metavar="FILE", help="Rulebook JSON (default: bundled v0.1).")
    parser.add_argument("--config", metavar="FILE", help="Detector configuration JSON.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scaletrace", description="Identify and explain scalability risks in Python/FastAPI backends.")
    parser.add_argument("--version", action="version", version=f"scaletrace {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Statically analyze a repository (target code is parsed, never executed).")
    analyze.add_argument("path")
    analyze.add_argument("--json", dest="json_out", metavar="FILE", help="Write the JSON report to FILE.")
    analyze.add_argument("--quiet", action="store_true", help="Do not print the text summary.")
    _add_static_options(analyze)

    graph = sub.add_parser("graph", help="Build the knowledge graph (graph.json + graph.html).")
    graph.add_argument("path")
    graph.add_argument("--out", default=DEFAULT_OUT, help=f"Output directory (default: {DEFAULT_OUT}).")
    graph.add_argument("--runtime", metavar="FILE", help="Merge observed edges from a runtime report.")
    graph.add_argument("--all-nodes", action="store_true", help="Show functions not reachable from any endpoint too.")
    _add_static_options(graph)

    run = sub.add_parser("run", help="Run a controlled workload against the target. EXECUTES target code.")
    run.add_argument("path")
    run.add_argument("--allow-execute", action="store_true", help="Required: confirm that target code may be executed.")
    run.add_argument("--workload", metavar="FILE", help="Workload spec (default: PATH/scaletrace_workload.json).")
    run.add_argument("--out", default=DEFAULT_OUT)

    diagnose = sub.add_parser("diagnose", help="Correlate evidence and explain symptoms; writes JSON, Markdown and HTML reports.")
    diagnose.add_argument("path")
    diagnose.add_argument("--runtime", metavar="FILE", help="Use an existing runtime report.")
    diagnose.add_argument("--run", action="store_true", help="Run the workload first (needs --allow-execute).")
    diagnose.add_argument("--allow-execute", action="store_true")
    diagnose.add_argument("--workload", metavar="FILE")
    diagnose.add_argument("--endpoint", action="append", default=[], help="Diagnose a reported symptom on this endpoint, e.g. 'GET /orders'.")
    diagnose.add_argument("--resource", choices=["database"], help="Diagnose a reported resource symptom (e.g. high DB load).")
    diagnose.add_argument("--model", metavar="FILE", help="Trained GNN ranker (from `scaletrace ml train`).")
    diagnose.add_argument("--diagnosis-config", metavar="FILE", help="DiagnosisConfig JSON (weights, thresholds).")
    diagnose.add_argument("--out", default=DEFAULT_OUT)
    _add_static_options(diagnose)

    evaluate = sub.add_parser("evaluate", help="Score findings against ground truth (legacy files or benchmark manifests).")
    evaluate.add_argument("paths", nargs="+", help="App directories (with scaletrace_groundtruth.json, or manifests with --manifest).")
    evaluate.add_argument("--csv", metavar="FILE", help="Export per-rule/category metrics as CSV.")
    evaluate.add_argument("--manifest", action="store_true", help="Use scaletrace_benchmark.json; only fully verified defects count.")
    evaluate.add_argument("--final", action="store_true", help="Allow locked test-split apps (logged in final_runs.log).")
    evaluate.add_argument("--level", choices=["both", "all", "recorded", "source", "runtime"], default="both",
                          help="Manifest mode: which verification tier counts as positive (default: report both, separately).")
    _add_static_options(evaluate)

    rules = sub.add_parser("rules", help="List rulebook rules and whether a detector exists.")
    rules.add_argument("--rulebook", metavar="FILE")

    sub.add_parser("schema", help="Print the JSON Schema of the analysis report.")

    from scaletrace.benchmark.cli import add_benchmark_parser
    from scaletrace.ml.cli import add_ml_parser

    add_benchmark_parser(sub)
    add_ml_parser(sub)
    return parser


def _static(args: argparse.Namespace):
    rulebook = load_rulebook(args.rulebook)
    config = load_config(args.config)
    rule_ids = [r.strip() for r in args.rules.split(",") if r.strip()] if args.rules else None
    return run_static_analysis(args.path, rulebook=rulebook, config=config, rule_ids=rule_ids)


def _cmd_analyze(args: argparse.Namespace) -> int:
    report = _static(args).report
    if not args.quiet:
        print(render_text(report))
    if args.json_out:
        print(f"JSON report written to {write_json_report(report, args.json_out)}")
    return EXIT_OK


def _load_runtime(path: str):
    from scaletrace.schemas.runtime import RuntimeReport

    return RuntimeReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _cmd_graph(args: argparse.Namespace) -> int:
    from scaletrace.graph import build_knowledge_graph, graph_to_json, merge_runtime, risk_paths
    from scaletrace.graph.html import render_graph_html

    static = _static(args)
    graph = build_knowledge_graph(static.project, static.report.findings)
    if args.runtime:
        merge_runtime(graph, _load_runtime(args.runtime))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph_to_json(graph), indent=2), encoding="utf-8")
    (out / "graph.html").write_text(
        render_graph_html(graph, static.report.findings, static.report.repository, only_relevant=not args.all_nodes), encoding="utf-8"
    )
    print(f"Knowledge graph: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")
    print("Risk paths (endpoint -> code with findings):")
    for path in risk_paths(graph) or []:
        print(f"  [{path.max_severity.upper()}] {path.describe(graph)}")
    print(f"Written: {out / 'graph.json'}, {out / 'graph.html'}")
    return EXIT_OK


def _run_runtime(path: str, workload: str | None, out: Path):
    from scaletrace.runtime import WORKLOAD_FILE, load_workload
    from scaletrace.runtime.runner import run_workload

    spec = load_workload(workload or Path(path) / WORKLOAD_FILE)
    print(EXECUTION_WARNING.format(path=path), file=sys.stderr)
    print(f"Running workload: sizes {spec.sizes}, {len(spec.requests)} request(s) x {spec.repetitions} ...", file=sys.stderr)
    runtime = run_workload(path, spec, out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "runtime.json").write_text(runtime.model_dump_json(indent=2), encoding="utf-8")
    return runtime


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.allow_execute:
        print("error: runtime testing executes target code; re-run with --allow-execute to confirm.", file=sys.stderr)
        return EXIT_USAGE
    out = Path(args.out)
    runtime = _run_runtime(args.path, args.workload, out)
    print(f"Runtime run {runtime.status} in {runtime.duration_s}s -> {out / 'runtime.json'}")
    if runtime.error:
        print(runtime.error, file=sys.stderr)
    for endpoint in runtime.endpoints:
        for m in endpoint.by_size():
            q = "n/a" if m.queries_per_request is None else f"{m.queries_per_request:.0f}"
            p95 = "n/a" if m.latency_ms_p95 is None else f"{m.latency_ms_p95:.1f}"
            print(f"  {endpoint.endpoint:<40} size={m.size:<6} queries/req={q:<7} p95={p95} ms errors={m.errors}")
    return EXIT_OK if runtime.status == "completed" else EXIT_RUNTIME_FAILED


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from scaletrace.diagnosis import DiagnosisConfig, diagnose_repository, user_symptom
    from scaletrace.graph import graph_to_json
    from scaletrace.graph.html import render_graph_html
    from scaletrace.reports.diagnosis_report import render_diagnosis_html, render_diagnosis_markdown

    out = Path(args.out)
    if args.run and not args.allow_execute:
        print("error: --run executes target code; add --allow-execute to confirm.", file=sys.stderr)
        return EXIT_USAGE
    static = _static(args)
    runtime = _run_runtime(args.path, args.workload, out) if args.run else (_load_runtime(args.runtime) if args.runtime else None)
    reported = [user_symptom(endpoint=e) for e in args.endpoint]
    if args.resource:
        reported.append(user_symptom(resource=args.resource))
    config = DiagnosisConfig.model_validate_json(Path(args.diagnosis_config).read_text(encoding="utf-8")) if args.diagnosis_config else DiagnosisConfig()

    ranker, ranker_name, explainer = None, "gnn", None
    if args.model:
        from scaletrace.ml.inference import load_ranker

        ranker, explainer = load_ranker(args.model, static.project, runtime)
        ranker_name = "gnn (learned)"
    report, graph = diagnose_repository(static, runtime, config, reported, ranker=ranker, ranker_name=ranker_name)
    if explainer is not None:
        for diagnosis in report.diagnoses:
            if diagnosis.candidates:
                diagnosis.explanation = explainer(graph, diagnosis.symptom, diagnosis.candidates[0].node)

    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(report.static_report.model_dump_json(indent=2), encoding="utf-8")
    (out / "diagnosis.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "graph.json").write_text(json.dumps(graph_to_json(graph), indent=2), encoding="utf-8")
    (out / "graph.html").write_text(render_graph_html(graph, report.static_report.findings, report.repository), encoding="utf-8")
    (out / "report.md").write_text(render_diagnosis_markdown(report, graph), encoding="utf-8")
    (out / "report.html").write_text(render_diagnosis_html(report, graph), encoding="utf-8")

    print(f"ScaleTrace diagnosis - {report.repository} (runtime evidence: {'yes' if report.runtime_available else 'no'})")
    print(f"Findings by status: {report.findings_by_status}")
    for diagnosis in report.diagnoses:
        print(f"\n[{diagnosis.status.upper()}{', approximate' if diagnosis.approximate else ''}] {diagnosis.symptom.description}")
        print(f"  path: {' -> '.join(diagnosis.path_labels)}")
        print(f"  {diagnosis.probable_cause}")
    if not report.diagnoses:
        print("No symptoms to diagnose (no runtime anomalies and no --endpoint/--resource given).")
    print(f"\nWritten to {out}: diagnosis.json, analysis.json, graph.json, graph.html, report.md, report.html")
    return EXIT_OK


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from scaletrace.evaluation import load_groundtruth
    from scaletrace.evaluation.metrics import GROUNDTRUTH_FILE, evaluate, render_table, write_csv

    rulebook = load_rulebook(args.rulebook)
    results = {}
    unverified: dict[str, int] = {}
    levels = {"both": ["source", "runtime"], "all": ["recorded", "source", "runtime"]}.get(args.level, [args.level])
    tiered: dict[str, dict] = {level: {} for level in levels}
    if args.manifest:
        from scaletrace.benchmark.guard import check_access, classify, record_final_run
        from scaletrace.benchmark.loader import groundtruth_from_manifest, load_manifest, unverified_defects

        check_access(args.paths, "final" if args.final else "diagnostic")
    for path in args.paths:
        args.path = path
        if args.manifest:
            manifest = load_manifest(path)
            static = _static(args).report
            for level in levels:
                tiered[level][manifest.app_id] = (static.findings, groundtruth_from_manifest(manifest, static.rules_applied, level))
            results[manifest.app_id] = tiered[levels[0]][manifest.app_id]
            unverified[manifest.app_id] = len(unverified_defects(manifest, "source"))
            continue
        truth_file = Path(path) / GROUNDTRUTH_FILE
        if not truth_file.exists():
            print(f"error: {truth_file} not found", file=sys.stderr)
            return EXIT_USAGE
        results[Path(path).name] = (_static(args).report.findings, load_groundtruth(truth_file))
    if args.manifest and args.final:
        located = [classify(p)[2] for p in args.paths]
        for root in sorted({loc[0] for loc in located if loc is not None}):
            print(f"Final evaluation logged in {record_final_run(root, [], list(results), 'scaletrace evaluate --manifest --final')}")
    if args.manifest:
        pending = ", ".join(f"{a}={n}" for a, n in unverified.items() if n)
        print(f"Defects not source-confirmed (never counted): {pending or 'none'}")
        for level in levels:
            title = {"recorded": "RECORDED-LABELS tier (frozen labels as drafted; not independently verified)",
                     "source": "SOURCE-CONFIRMED tier (pattern confirmed in code; says nothing about runtime impact)",
                     "runtime": "RUNTIME-VERIFIED tier (also confirmed at runtime, or no runtime metric exists)"}[level]
            print(f"\n=== {title} ===")
            evaluation = evaluate(tiered[level], rulebook)
            _print_evaluation(evaluation)
            if args.csv:
                target = Path(args.csv)
                if len(levels) > 1:
                    target = target.with_name(f"{target.stem}_{level}{target.suffix}")
                print(f"\nCSV written to {write_csv(evaluation, target)}")
        return EXIT_OK
    evaluation = evaluate(results, rulebook)
    _print_evaluation(evaluation)
    if args.csv:
        print(f"\nCSV written to {write_csv(evaluation, args.csv)}")
    return EXIT_OK


def _print_evaluation(evaluation) -> None:
    from scaletrace.evaluation.metrics import render_table

    for app, match in evaluation.per_app.items():
        print(f"{app}: TP={len(match.true_positives)} FP={len(match.false_positives)} FN={len(match.false_negatives)}")
        for f in match.false_positives:
            print(f"    FP {f.rule_id} {f.file}:{f.line} {f.function}")
        for e in match.false_negatives:
            print(f"    FN {e.rule_id} {e.file}:{e.line} {e.function}")
    print()
    print(render_table(evaluation))


def _cmd_rules(args: argparse.Namespace) -> int:
    rulebook = load_rulebook(args.rulebook)
    print(f"Rulebook {rulebook.rulebook_version} ({rulebook.status})")
    for rule in rulebook.rules:
        state = "implemented" if rule.id in DETECTORS else "planned"
        print(f"  {rule.id:<8} {rule.status:<12} {rule.severity.value:<6} {state:<11} {rule.name}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = _build_parser().parse_args(argv)
    handlers = {
        "analyze": _cmd_analyze,
        "graph": _cmd_graph,
        "run": _cmd_run,
        "diagnose": _cmd_diagnose,
        "evaluate": _cmd_evaluate,
        "rules": _cmd_rules,
    }
    try:
        if args.command == "schema":
            print(report_json_schema())
            return EXIT_OK
        if args.command == "ml":
            return args.ml_handler(args)
        if args.command == "benchmark":
            return args.benchmark_handler(args)
        return handlers[args.command](args)
    except (RepositoryError, RulebookError, ConfigError, UnknownRuleError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
