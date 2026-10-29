"""`scaletrace benchmark ...`: init, validate, split, lock, summary. All static; nothing is executed."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2


def add_benchmark_parser(sub) -> None:
    bench = sub.add_parser("benchmark", help="Benchmark manifests: init, validate, split, lock, summary (static only).")
    bench_sub = bench.add_subparsers(dest="benchmark_command", required=True)

    init = bench_sub.add_parser("init", help="Write a blank manifest for one application and print static facts for reference.")
    init.add_argument("app_dir")
    init.add_argument("--kind", choices=["good", "bad"], required=True)
    init.add_argument("--source", choices=["team", "public", "example"], default="team")
    init.add_argument("--app-id", help="Default: the directory name.")
    init.add_argument("--author")
    init.add_argument("--template-family")
    init.add_argument("--force", action="store_true", help="Overwrite an existing manifest.")
    init.set_defaults(benchmark_handler=_cmd_init)

    validate = bench_sub.add_parser("validate", help="Check manifests, labels, splits and the lock.")
    validate.add_argument("root")
    validate.add_argument("--strict", action="store_true", help="Treat warnings as errors.")
    validate.add_argument("--rulebook", metavar="FILE")
    validate.add_argument("--json", metavar="FILE", help="Write all issues as JSON.")
    validate.set_defaults(benchmark_handler=_cmd_validate)

    split = bench_sub.add_parser("split", help="Assign grouped, stratified dev/test splits (writes benchmark_index.json).")
    split.add_argument("root")
    split.add_argument("--dev", type=float, default=0.2, help="Fraction of good and of bad apps for dev (default 0.2).")
    split.add_argument("--seed", type=int, default=0)
    split.add_argument("--group-by", nargs="+", default=["template_family", "derived_from"],
                       help="Fields that keep apps together: template_family derived_from author.")
    split.set_defaults(benchmark_handler=_cmd_split)

    lock = bench_sub.add_parser("lock", help="Freeze the test split (hashes of manifests and sources).")
    lock.add_argument("root")
    lock.set_defaults(benchmark_handler=_cmd_lock)

    summary = bench_sub.add_parser("summary", help="Counts by kind, split, rule and verification status.")
    summary.add_argument("root")
    summary.set_defaults(benchmark_handler=_cmd_summary)

    imp = bench_sub.add_parser("import-register", help="Import the team's screening CSVs into a candidate registry (no labels).")
    imp.add_argument("--good", metavar="CSV")
    imp.add_argument("--bad", metavar="CSV")
    imp.add_argument("--registry", required=True, metavar="FILE", help="benchmark_registry.json to write/update.")
    imp.add_argument("--source-url", default="")
    imp.add_argument("--source-commit", default="")
    imp.set_defaults(benchmark_handler=_cmd_import)

    fetch = bench_sub.add_parser("fetch", help="Pin + download candidates for STATIC analysis (never executed); draft manifests.")
    fetch.add_argument("--registry", required=True, metavar="FILE")
    fetch.add_argument("--apps-dir", required=True, help="Where to place fetched apps (the benchmark root).")
    fetch.add_argument("--only", nargs="+", help="Candidate ids to fetch.")
    fetch.add_argument("--include-rejected", action="store_true")
    fetch.add_argument("--force", action="store_true", help="Re-download even if already fetched.")
    fetch.set_defaults(benchmark_handler=_cmd_fetch)

    sheet = bench_sub.add_parser("review-sheet", help="Write per-app review worksheets (.md to read, .json to fill in).")
    sheet.add_argument("--registry", required=True, metavar="FILE")
    sheet.add_argument("--apps-dir", required=True)
    sheet.add_argument("--out", required=True, help="Directory for review_<app>.md / .json.")
    sheet.add_argument("--only", nargs="+", help="App ids or candidate ids.")
    sheet.add_argument("--force", action="store_true", help="Overwrite existing decisions files (loses edits!).")
    sheet.set_defaults(benchmark_handler=_cmd_review_sheet)

    apply = bench_sub.add_parser("apply-review", help="Copy a reviewer's explicit decisions into the manifest, then validate.")
    apply.add_argument("reviews", nargs="+", help="review_<app>.json files.")
    apply.add_argument("--apps-dir", required=True)
    apply.add_argument("--rulebook", metavar="FILE")
    apply.set_defaults(benchmark_handler=_cmd_apply_review)

    final = bench_sub.add_parser("evaluate-final", help="Final static evaluation on the LOCKED test split (logged); --dry-run uses dev.")
    final.add_argument("root")
    final.add_argument("--models", nargs="*", default=[], help="Frozen model checkpoints to include.")
    final.add_argument("--out", required=True)
    final.add_argument("--dry-run", action="store_true", help="Rehearse on the dev split; nothing is logged as final.")
    final.add_argument("--rulebook", metavar="FILE")
    final.set_defaults(benchmark_handler=_cmd_final)

    screen = bench_sub.add_parser("screen", help="Static screening report of fetched candidates (reviewer aid, not labels).")
    screen.add_argument("--registry", required=True, metavar="FILE")
    screen.add_argument("--apps-dir", required=True)
    screen.add_argument("--out", metavar="FILE", help="Markdown report path.")
    screen.set_defaults(benchmark_handler=_cmd_screen)


def run(args) -> int:
    return args.benchmark_handler(args)


def _cmd_init(args) -> int:
    from scaletrace.benchmark.schema import MANIFEST_FILE, AppManifest, Origin
    from scaletrace.pipeline import run_static_analysis

    app_dir = Path(args.app_dir)
    target = app_dir / MANIFEST_FILE
    if target.exists() and not args.force:
        print(f"error: {target} exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_USAGE
    manifest = AppManifest(
        app_id=args.app_id or app_dir.resolve().name,
        kind=args.kind,
        origin=Origin(source=args.source, author=args.author, template_family=args.template_family),
    )
    target.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    static = run_static_analysis(app_dir)
    print(f"Wrote {target} with no defects. Add each defect by hand (see docs/benchmark.md).")
    print("\nReference only -- routes and ScaleTrace findings. Do NOT copy findings into the manifest without")
    print("independent verification; ground truth must not be derived from the tool being evaluated.\n")
    for endpoint, info in sorted(static.project.endpoints().items()):
        print(f"  route    {endpoint:<40} -> {info.func_id} ({info.file}:{info.line}-{info.end_line})")
    for finding in static.report.findings:
        print(f"  finding  {finding.rule_id:<8} {finding.file}:{finding.line} in {finding.function}")
    return EXIT_OK


def _cmd_validate(args) -> int:
    from scaletrace.benchmark.validate import validate_benchmark
    from scaletrace.rules import load_rulebook

    report = validate_benchmark(args.root, load_rulebook(args.rulebook))
    for issue in sorted(report.issues, key=lambda i: (i.level != "error", i.app_id, i.code)):
        print(f"{issue.level.upper():<8} {issue.app_id:<24} {issue.code:<24} {issue.message}")
    print(f"\n{len(report.apps)} app(s): {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    if args.json:
        Path(args.json).write_text(json.dumps([vars(i) for i in report.issues], indent=2), encoding="utf-8")
    failed = report.errors or (args.strict and report.warnings)
    print("RESULT: " + ("INVALID" if failed else "VALID"))
    return EXIT_INVALID if failed else EXIT_OK


def _load_all(root: Path):
    from scaletrace.benchmark.loader import find_manifest_dirs, load_manifest

    apps = {}
    for app_dir in find_manifest_dirs(root):
        manifest = load_manifest(app_dir)
        if manifest.app_id in apps:
            raise ValueError(f"duplicate app_id {manifest.app_id}")
        apps[manifest.app_id] = (app_dir, manifest)
    return apps


def _cmd_split(args) -> int:
    from scaletrace.benchmark.loader import save_index
    from scaletrace.benchmark.split import build_index

    root = Path(args.root)
    index = build_index(root, _load_all(root), args.dev, args.seed, args.group_by)
    save_index(root, index)
    counts = Counter((e.split, e.kind) for e in index.apps)
    print(f"Wrote {root / 'benchmark_index.json'} (seed {args.seed}, dev fraction {args.dev}, group by {args.group_by})")
    for split in ("dev", "test", "excluded"):
        print(f"  {split:<9} good={counts[(split, 'good')]:<3} bad={counts[(split, 'bad')]}")
    return EXIT_OK


def _cmd_lock(args) -> int:
    from scaletrace.benchmark.loader import load_index
    from scaletrace.benchmark.schema import SIGNOFF_LOG
    from scaletrace.benchmark.split import lock_index

    root = Path(args.root)
    index = load_index(root)
    if index is None:
        print("error: no benchmark_index.json; run `scaletrace benchmark split` first", file=sys.stderr)
        return EXIT_USAGE
    index = lock_index(root, index)
    print(f"Locked {len(index.lock.apps)} test app(s) at {index.lock.locked_at}. Labels and sources are frozen; only verification sign-offs via apply-review are allowed, and each is logged in {SIGNOFF_LOG}.")
    return EXIT_OK


def _cmd_import(args) -> int:
    from scaletrace.benchmark.register import import_registers, load_registry, readiness_report, save_registry

    if not args.good and not args.bad:
        print("error: give --good and/or --bad", file=sys.stderr)
        return EXIT_USAGE
    previous = load_registry(args.registry)
    source = {k: v for k, v in (("url", args.source_url), ("commit", args.source_commit)) if v}
    registry = import_registers(args.good, args.bad, source, previous)
    path = save_registry(args.registry, registry)
    report = Path(args.registry).with_name("readiness_report.md")
    report.write_text(readiness_report(registry), encoding="utf-8")
    counts = Counter((c.intended_kind, c.decision) for c in registry.candidates)
    print(f"Imported {len(registry.candidates)} candidates -> {path} (report: {report})")
    for (kind, decision), n in sorted(counts.items()):
        print(f"  {kind:<5} {decision:<22} {n}")
    claims = sum(len(c.suspected_findings) for c in registry.candidates)
    print(f"Suspected findings recorded as UNVERIFIED claims: {claims}. No manifests or labels were created.")
    return EXIT_OK


def _cmd_fetch(args) -> int:
    from scaletrace.benchmark.fetch import fetch_registry
    from scaletrace.benchmark.register import load_registry, readiness_report, save_registry

    registry = load_registry(args.registry)
    if registry is None:
        print(f"error: {args.registry} not found; run import-register first", file=sys.stderr)
        return EXIT_USAGE
    print("Downloading repositories for STATIC analysis only. Nothing is installed or executed.")
    fetch_registry(registry, Path(args.apps_dir), set(args.only) if args.only else None, args.include_rejected, args.force)
    save_registry(args.registry, registry)
    Path(args.registry).with_name("readiness_report.md").write_text(readiness_report(registry), encoding="utf-8")
    counts = Counter(c.fetch.status for c in registry.candidates)
    print(f"Fetch status: {dict(counts)}")
    return EXIT_OK


def _cmd_review_sheet(args) -> int:
    from scaletrace.benchmark.register import load_registry
    from scaletrace.benchmark.review import write_review

    registry = load_registry(args.registry)
    if registry is None:
        print(f"error: {args.registry} not found", file=sys.stderr)
        return EXIT_USAGE
    only = {o.lower() for o in args.only} if args.only else None
    written = skipped = 0
    for candidate in registry.candidates:
        if candidate.fetch.status != "fetched":
            continue
        if only and candidate.app_id not in only and candidate.candidate_id.lower() not in only:
            continue
        result = write_review(candidate, Path(args.apps_dir) / candidate.app_id, Path(args.out), args.force)
        if result is None:
            skipped += 1
            print(f"  {candidate.app_id:<14} kept existing decisions file (use --force to regenerate)")
        else:
            written += 1
            print(f"  {candidate.app_id:<14} -> {result[0].name}, {result[1].name}")
    print(f"Wrote {written} worksheet(s); kept {skipped} existing decisions file(s).")
    return EXIT_OK


def _cmd_apply_review(args) -> int:
    from scaletrace.benchmark.review import ReviewError, apply_review
    from scaletrace.benchmark.validate import validate_benchmark
    from scaletrace.rules import load_rulebook

    rulebook = load_rulebook(args.rulebook)
    failed = 0
    for path in args.reviews:
        try:
            summary = apply_review(path, Path(args.apps_dir), rulebook)
        except ReviewError as exc:
            failed += 1
            print(f"NOT APPLIED {path}: {exc}", file=sys.stderr)
            continue
        print(f"Applied {path}: {summary['defects']} defect(s) ({summary['source_confirmed']} source-confirmed), "
              f"{summary['known_non_defects']} known non-defect(s), licence {summary['licence']}"
              f"{', EXCLUDED' if summary['excluded'] else ''}; {len(summary['open_items'])} open item(s)")
        for item in summary["open_items"]:
            print(f"    open: {item}")
        report = validate_benchmark(Path(args.apps_dir) / summary["app_id"], rulebook, check_duplicates=False)
        for issue in report.issues:
            print(f"    {issue.level.upper()} {issue.code}: {issue.message}")
    return EXIT_INVALID if failed else EXIT_OK


def _cmd_final(args) -> int:
    from scaletrace.benchmark.final import FinalEvaluationError, run_final
    from scaletrace.rules import load_rulebook

    try:
        results = run_final(args.root, load_rulebook(args.rulebook), args.models, args.out, dry_run=args.dry_run)
    except FinalEvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    print(f"{results['kind']}: {len(results['apps'])} apps; report in {Path(args.out) / 'final_report.md'}")
    return EXIT_OK


def _cmd_screen(args) -> int:
    from scaletrace.benchmark.register import load_registry
    from scaletrace.benchmark.screen import screening_report

    registry = load_registry(args.registry)
    if registry is None:
        print(f"error: {args.registry} not found", file=sys.stderr)
        return EXIT_USAGE
    report = screening_report(registry, Path(args.apps_dir))
    out = Path(args.out) if args.out else Path(args.registry).with_name("screening_report.md")
    out.write_text(report, encoding="utf-8")
    print(f"Screening report written to {out}")
    return EXIT_OK


def _cmd_summary(args) -> int:
    from scaletrace.benchmark.loader import load_index, positive_defects, unverified_defects

    root = Path(args.root)
    apps = _load_all(root)
    index = load_index(root)
    split_of = {e.app_id: e.split for e in index.apps} if index else {}
    by_split = Counter((split_of.get(a, m.split), m.kind) for a, (_, m) in apps.items())
    by_rule = Counter()
    for _, manifest in apps.values():
        for defect in manifest.defects:
            v = defect.verification
            state = "rejected" if v.rejected else ("runtime-verified" if v.fully_verified else ("source-confirmed" if v.source_confirmed else "unverified"))
            by_rule[(defect.rule_id, state)] += 1
    print(f"{len(apps)} application(s); index: {'locked' if index and index.lock else ('present' if index else 'none')}")
    for (split, kind), n in sorted(by_split.items()):
        print(f"  {split:<10} {kind:<5} {n}")
    print("Defects by rule and verification status:")
    for (rule, state), n in sorted(by_rule.items()):
        print(f"  {rule:<8} {state:<10} {n}")
    source = sum(len(positive_defects(m, "source")) for _, m in apps.values())
    runtime = sum(len(positive_defects(m, "runtime")) for _, m in apps.values())
    pending = sum(len(unverified_defects(m, "source")) for _, m in apps.values())
    print(f"Positives: source-confirmed tier {source}; runtime-verified tier {runtime}; not yet source-confirmed {pending}")
    return EXIT_OK
