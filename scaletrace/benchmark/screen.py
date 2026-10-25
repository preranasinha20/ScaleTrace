"""Static screening of fetched candidates (parsing only; nothing is executed).

The report helps reviewers write manifests. It is NOT ground truth: ScaleTrace's findings are shown
for reference, and the team's claims are only located (does the file/line still exist at the pinned
commit, which function contains it, does a ScaleTrace rule fire there).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from scaletrace.benchmark.register import Registry
from scaletrace.pipeline import run_static_analysis


def _containing_function(project, file: str, line: int):
    best = None
    for info in project.functions.values():
        if info.file != file or info.qualname == "<module>" or info.end_line is None:
            continue
        if info.line <= line <= info.end_line and (best is None or info.end_line - info.line < best.end_line - best.line):
            best = info
    return best


def screen_candidate(app_dir: Path, claims) -> dict:
    static = run_static_analysis(app_dir)
    project = static.project
    by_rule = Counter(f.rule_id for f in static.report.findings)
    located = []
    for claim in claims:
        entry = {"rules": claim.rule_ids, "file": claim.file, "line": claim.line_start, "status": "no location in claim"}
        if claim.file:
            if not (app_dir / claim.file).is_file():
                entry["status"] = "file not present at pinned commit"
            elif claim.line_start is None:
                entry["status"] = "file present (no line given)"
            else:
                info = _containing_function(project, claim.file, claim.line_start)
                hits = [f.rule_id for f in static.report.findings
                        if f.file == claim.file and claim.line_start <= f.line <= (claim.line_end or claim.line_start)]
                entry.update({
                    "status": "located" if info else "line not inside a function",
                    "function": info.qualname if info else None,
                    "func_id": info.func_id if info else None,
                    "scaletrace_rules_on_lines": sorted(set(hits)),
                })
        located.append(entry)
    return {
        "files_analyzed": static.report.files_analyzed,
        "files_skipped": len(static.report.files_skipped),
        "routes": len(project.endpoints()),
        "findings_by_rule": dict(sorted(by_rule.items())),
        "claims": located,
    }


def screening_report(registry: Registry, apps_dir: Path, log=print) -> str:
    lines = [
        "# Static screening of fetched candidates", "",
        "Parsing only; no repository was executed. ScaleTrace findings are for reviewer reference and are",
        "**not** labels. Claims are the screening team's unverified notes, located at the pinned commit.", "",
        "| Candidate | Kind | Decision | Commit | .py files | Routes | ScaleTrace findings (reference) | Claims located |",
        "|---|---|---|---|---|---|---|---|",
    ]
    details = []
    for candidate in registry.candidates:
        if candidate.fetch.status != "fetched":
            continue
        app_dir = apps_dir / candidate.app_id
        log(f"  screening {candidate.candidate_id} ({candidate.repository})")
        result = screen_candidate(app_dir, candidate.suspected_findings)
        findings = ", ".join(f"{r}:{n}" for r, n in result["findings_by_rule"].items()) or "none"
        claims = "; ".join(f"{','.join(c['rules']) or '?'} {c['file'] or ''}:{c['line'] or ''} -> {c['status']}"
                           f"{' in ' + c['function'] if c.get('function') else ''}"
                           f"{' (rule hit: ' + ','.join(c['scaletrace_rules_on_lines']) + ')' if c.get('scaletrace_rules_on_lines') else ''}"
                           for c in result["claims"]) or "-"
        lines.append(f"| {candidate.candidate_id} | {candidate.intended_kind} | {candidate.decision} | {(candidate.fetch.commit or '')[:10]} | "
                     f"{result['files_analyzed']} | {result['routes']} | {findings} | {claims} |")
        details.append((candidate, result))
    no_routes = [c.candidate_id for c, r in details if r["routes"] == 0]
    lines += ["", f"Fetched candidates without any detected FastAPI route: {', '.join(no_routes) or 'none'}."]
    return "\n".join(lines) + "\n"
