"""Human-readable terminal summary. ASCII only, so it renders on any Windows console."""

from __future__ import annotations

from scaletrace.schemas import AnalysisReport


def render_text(report: AnalysisReport) -> str:
    lines = [
        f"ScaleTrace {report.scaletrace_version} - rulebook {report.rulebook_version} ({report.rulebook_status})",
        f"Repository: {report.repository}",
        f"Python files analyzed: {report.files_analyzed}, skipped: {len(report.files_skipped)}",
        f"Rules applied: {', '.join(report.rules_applied) or 'none'}",
        "",
    ]
    if not report.findings:
        lines.append("No findings for the applied rules.")
    for finding in report.findings:
        location = f"{finding.file}:{finding.line}"
        where = f"in {finding.function}" + (f" ({finding.endpoint})" if finding.endpoint else "")
        lines.append(f"[{finding.severity.value.upper()}] {finding.rule_id} {finding.rule_name}")
        lines.append(f"    {location} {where}")
        lines.append(f"    status: {finding.status.value}")
        for evidence in finding.static_evidence:
            lines.append(f"    code:   {evidence.snippet}")
            lines.append(f"    why:    {evidence.description}")
        lines.append("")
    if report.files_skipped:
        lines.append("Skipped files:")
        lines.extend(f"    {skipped.file}: {skipped.reason}" for skipped in report.files_skipped)
        lines.append("")
    lines.append(f"{len(report.findings)} finding(s). Static findings are potential risks; runtime confirmation is pending.")
    return "\n".join(lines)
