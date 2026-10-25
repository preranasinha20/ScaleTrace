"""Readable diagnostic reports (Markdown and HTML) built from a DiagnosisReport."""

from __future__ import annotations

import html

import networkx as nx

from scaletrace.graph.knowledge_graph import risk_paths
from scaletrace.schemas import Finding
from scaletrace.schemas.diagnosis import Diagnosis, DiagnosisReport

STATUS_TEXT = {
    "correlated": "Correlated: static and runtime evidence agree",
    "static_only": "Static evidence only (not runtime-confirmed)",
    "unexplained": "Unexplained: no source mapping found",
}


def _findings(report: DiagnosisReport) -> dict[str, Finding]:
    return {f.finding_id: f for f in report.static_report.findings}


def _diagnosis_md(diagnosis: Diagnosis, findings: dict[str, Finding]) -> list[str]:
    related = [findings[f] for f in diagnosis.related_findings if f in findings]
    lines = [
        f"### {diagnosis.symptom.endpoint or diagnosis.symptom.resource or 'Symptom'}",
        "",
        f"**Status:** {STATUS_TEXT[diagnosis.status]}" + (" - approximate (missing runtime attribution or observed edges)" if diagnosis.approximate else ""),
        f"  \n**Ranking:** {diagnosis.ranking_method}",
        "",
        f"- **Observed symptom:** {diagnosis.symptom.description}",
    ]
    lines += [f"- **Also observed:** {s.description}" for s in diagnosis.supporting_symptoms]
    lines.append(f"- **Affected endpoint/resource:** {diagnosis.symptom.endpoint or diagnosis.symptom.resource}")
    if related:
        lines.append(f"- **Risk category / rule:** " + "; ".join(f"{f.category} / {f.rule_id} {f.rule_name}" for f in related))
        lines.append(f"- **Source location:** " + "; ".join(f"`{f.file}:{f.line}` in `{f.function}`" for f in related))
        lines.append(f"- **Severity:** " + ", ".join(sorted({f.severity.value for f in related})))
    lines += ["", f"**Diagnostic path:** `{' -> '.join(diagnosis.path_labels)}`", "", "**Step by step:**", ""]
    lines += [f"{s.order}. *({s.evidence})* {s.text}" for s in diagnosis.steps]
    lines += ["", f"**{diagnosis.probable_cause}**", ""]
    if diagnosis.candidates:
        lines += ["**Ranked candidates:**", "", "| Function | S | D | P | Risk | Learned | Rules |", "|---|---|---|---|---|---|---|"]
        for c in diagnosis.candidates:
            fmt = lambda v: "n/a" if v is None else f"{v:.2f}"
            lines.append(f"| `{c.label}` | {fmt(c.static_score)} | {fmt(c.dynamic_score)} | {fmt(c.importance)} | {c.risk:.2f} | {fmt(c.gnn_score)} | {', '.join(c.rules) or '-'} |")
        lines.append("")
    if diagnosis.explanation:
        lines += ["**Learned-model explanation (most influential edges/features):**", ""]
        for item in diagnosis.explanation.get("top_edges", [])[:5]:
            lines.append(f"- edge `{item['source']} -> {item['target']}` ({item['kind']}): importance {item['importance']:.2f}")
        for item in diagnosis.explanation.get("top_features", [])[:5]:
            lines.append(f"- feature `{item['feature']}`: importance {item['importance']:.2f}")
        lines.append("")
    if diagnosis.verification_steps:
        lines += ["**Suggested verification:**", ""] + [f"- {s}" for s in diagnosis.verification_steps] + [""]
    return lines


def render_diagnosis_markdown(report: DiagnosisReport, graph: nx.MultiDiGraph) -> str:
    findings = _findings(report)
    static = report.static_report
    lines = [
        f"# ScaleTrace report: {report.repository}",
        "",
        f"Generated {report.generated_at} by ScaleTrace {report.scaletrace_version} (rulebook {static.rulebook_version}, {static.rulebook_status}).",
        f"Runtime evidence: {'yes' if report.runtime_available else 'no (static analysis only)'}.",
        "",
        "## Summary",
        "",
        f"- Python files analyzed: {static.files_analyzed} (skipped: {len(static.files_skipped)})",
        f"- Findings: {len(static.findings)} - " + (", ".join(f"{k}: {v}" for k, v in sorted(report.findings_by_status.items())) or "none"),
        f"- Runtime symptoms: {len(report.symptoms)}; diagnoses: {len(report.diagnoses)}; unexplained observations: {len(report.unexplained_observations)}",
        "",
        "## Diagnoses",
        "",
    ]
    if not report.diagnoses:
        lines += ["No symptoms to diagnose. Provide a runtime report (`scaletrace run`) or a reported symptom (`--endpoint`).", ""]
    for diagnosis in report.diagnoses:
        lines += _diagnosis_md(diagnosis, findings)

    if report.unexplained_observations:
        lines += ["## Runtime observations without a source mapping", ""]
        lines += [f"- {o.symptom.description} {o.note}" for o in report.unexplained_observations]
        lines.append("")

    lines += ["## Where scaling problems can occur (knowledge graph risk paths)", ""]
    paths = risk_paths(graph)
    lines += [f"- `{p.describe(graph)}`" for p in paths] or ["- No endpoint reaches a function with findings."]
    lines += ["", "## All findings", "", "| Rule | Severity | Status | Location | Function | Endpoint(s) |", "|---|---|---|---|---|---|"]
    for f in static.findings:
        lines.append(f"| {f.rule_id} | {f.severity.value} | {f.status.value} | `{f.file}:{f.line}` | `{f.function}` | {f.endpoint or '-'} |")
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in report.limitations] + [""]
    return "\n".join(lines)


def render_diagnosis_html(report: DiagnosisReport, graph: nx.MultiDiGraph, graph_file: str | None = "graph.html") -> str:
    """A styled HTML version of the Markdown report (no external dependencies)."""
    findings = _findings(report)
    esc = html.escape
    parts = []
    for diagnosis in report.diagnoses:
        related = [findings[f] for f in diagnosis.related_findings if f in findings]
        steps = "".join(f"<li><span class='tag {esc(s.evidence)}'>{esc(s.evidence)}</span> {esc(s.text)}</li>" for s in diagnosis.steps)
        rows = "".join(
            f"<tr><td><code>{esc(c.label)}</code></td><td>{'' if c.static_score is None else f'{c.static_score:.2f}'}</td>"
            f"<td>{'n/a' if c.dynamic_score is None else f'{c.dynamic_score:.2f}'}</td><td>{'' if c.importance is None else f'{c.importance:.2f}'}</td>"
            f"<td><b>{c.risk:.2f}</b></td><td>{'n/a' if c.gnn_score is None else f'{c.gnn_score:.2f}'}</td><td>{esc(', '.join(c.rules) or '-')}</td></tr>"
            for c in diagnosis.candidates
        )
        verification = "".join(f"<li>{esc(v)}</li>" for v in diagnosis.verification_steps)
        locations = ", ".join(f"<code>{esc(f.file)}:{f.line}</code> ({esc(f.rule_id)})" for f in related) or "none"
        parts.append(f"""
<section class="card status-{esc(diagnosis.status)}">
  <h3>{esc(diagnosis.symptom.endpoint or diagnosis.symptom.resource or 'Symptom')}</h3>
  <p class="status">{esc(STATUS_TEXT[diagnosis.status])}{' &middot; approximate' if diagnosis.approximate else ''} &middot; ranking: {esc(diagnosis.ranking_method)}</p>
  <p><b>Symptom:</b> {esc(diagnosis.symptom.description)}</p>
  <p><b>Path:</b> <code>{esc(' -> '.join(diagnosis.path_labels))}</code></p>
  <p><b>Source:</b> {locations}</p>
  <ol class="steps">{steps}</ol>
  <p class="cause"><b>{esc(diagnosis.probable_cause)}</b></p>
  <table><thead><tr><th>Candidate</th><th>S</th><th>D</th><th>P</th><th>Risk</th><th>Learned</th><th>Rules</th></tr></thead><tbody>{rows}</tbody></table>
  {'<h4>Suggested verification</h4><ul>' + verification + '</ul>' if verification else ''}
</section>""")
    finding_rows = "".join(
        f"<tr><td>{esc(f.rule_id)}</td><td>{esc(f.severity.value)}</td><td>{esc(f.status.value)}</td><td><code>{esc(f.file)}:{f.line}</code></td>"
        f"<td><code>{esc(f.function)}</code></td><td>{esc(f.endpoint or '-')}</td></tr>"
        for f in report.static_report.findings
    )
    paths = "".join(f"<li><code>{esc(p.describe(graph))}</code></li>" for p in risk_paths(graph)) or "<li>None</li>"
    observations = "".join(f"<li>{esc(o.symptom.description)} {esc(o.note)}</li>" for o in report.unexplained_observations)
    limitations = "".join(f"<li>{esc(item)}</li>" for item in report.limitations)
    graph_link = f'<p><a href="{esc(graph_file)}">Open the interactive knowledge graph</a></p>' if graph_file else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScaleTrace report - {esc(report.repository)}</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --border:#dcdcd8; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17171a; --card:#222226; --text:#ececef; --muted:#a0a0a8; --border:#3a3a40; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }} }}
body {{ margin:0 auto; max-width:1100px; padding:16px; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }}
code {{ font-family:ui-monospace,Consolas,monospace; font-size:0.9em; overflow-wrap:anywhere; }}
.card {{ background:var(--card); border:1px solid var(--border); border-left:4px solid var(--muted); border-radius:8px; padding:12px 16px; margin:16px 0; }}
.status-correlated {{ border-left-color:var(--bad); }} .status-static_only {{ border-left-color:var(--warn); }} .status-unexplained {{ border-left-color:var(--muted); }}
.status {{ color:var(--muted); margin-top:-8px; }}
.tag {{ font-size:11px; border:1px solid var(--border); border-radius:4px; padding:0 4px; color:var(--muted); }}
.steps li {{ margin:4px 0; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; display:block; overflow-x:auto; }}
th, td {{ border-bottom:1px solid var(--border); padding:4px 8px; text-align:left; }}
</style></head><body>
<h1>ScaleTrace report: {esc(report.repository)}</h1>
<p>Generated {esc(report.generated_at)} &middot; runtime evidence: {'yes' if report.runtime_available else 'no (static only)'} &middot;
findings: {len(report.static_report.findings)} ({esc(', '.join(f'{k}: {v}' for k, v in sorted(report.findings_by_status.items())))})</p>
{graph_link}
<h2>Diagnoses</h2>
{''.join(parts) or '<p>No symptoms to diagnose.</p>'}
{'<h2>Runtime observations without a source mapping</h2><ul>' + observations + '</ul>' if observations else ''}
<h2>Where scaling problems can occur</h2><ul>{paths}</ul>
<h2>All findings</h2>
<table><thead><tr><th>Rule</th><th>Severity</th><th>Status</th><th>Location</th><th>Function</th><th>Endpoint(s)</th></tr></thead><tbody>{finding_rows}</tbody></table>
<h2>Limitations</h2><ul>{limitations}</ul>
</body></html>
"""
