"""Correlating static findings with runtime measurements.

Each rule names the runtime metric that can support it. A finding becomes:

* ``correlated_finding`` -- an endpoint that reaches the finding was measured, the finding's
  function executed (when call tracing is on), and the rule's metric grew with input size;
* ``not_confirmed`` -- the function executed under the workload but the metric did not grow;
* ``static_candidate`` (unchanged) -- no runtime evidence applies: the rule has no measurable
  metric yet, no measured endpoint reaches the function, or the function did not execute.

The reason is always recorded in ``runtime_evidence`` so the status is explainable.
"""

from __future__ import annotations

import re

from scaletrace.diagnosis.config import DiagnosisConfig
from scaletrace.diagnosis.symptoms import compute_latency_series
from scaletrace.runtime.analysis import describe_series, growth_class, growth_exponent
from scaletrace.schemas import Finding, FindingStatus, RuntimeEvidence
from scaletrace.schemas.runtime import EndpointRuntime, RuntimeReport

RULE_METRICS: dict[str, str | None] = {
    "DB-001": "queries",
    "DB-002": "queries",
    "DB-003": "queries",
    "CMP-001": "compute_latency",
    "CMP-002": "compute_latency",
    "MEM-001": "memory",
    "MEM-002": None,  # needs repeated-request memory tracking, not a size sweep
    "EXT-001": None,  # HTTP clients are not instrumented
}
UNMEASURABLE_REASON = {
    "MEM-002": "growth across repeated requests is not measured by the size-sweep workload",
    "EXT-001": "external HTTP calls are not instrumented",
}


def _template_regex(endpoint: str) -> re.Pattern[str]:
    method, _, path = endpoint.partition(" ")
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(path))
    return re.compile(f"^{re.escape(method)} {pattern}$")


def match_runtime_endpoint(static_endpoint: str, runtime: RuntimeReport) -> EndpointRuntime | None:
    for endpoint in runtime.endpoints:
        if endpoint.endpoint == static_endpoint:
            return endpoint
    regex = _template_regex(static_endpoint)
    for endpoint in runtime.endpoints:
        if regex.match(endpoint.endpoint):
            return endpoint
    return None


def involved_functions(finding: Finding, func_id: str) -> list[str]:
    involved = [func_id]
    for evidence in finding.static_evidence:
        involved.extend(f for f in evidence.details.get("call_chain", []) if f not in involved)
    return involved


def metric_series(metric: str, endpoint: EndpointRuntime, involved: list[str]) -> tuple[list[int], list[float | None], str, float]:
    measurements = endpoint.by_size()
    sizes = [m.size for m in measurements]
    if metric == "queries":
        values = [sum(m.queries_by_function_per_request.get(f, 0.0) for f in involved) for m in measurements]
        return sizes, values, "queries/request attributed to the finding's code", 1.0
    if metric == "compute_latency":
        return *compute_latency_series(endpoint), "ms (mean latency excluding SQL time)", 0.0
    if metric == "memory":
        return sizes, [m.rss_mb_peak for m in measurements], "MB (process RSS peak)", 0.0
    raise ValueError(metric)


def _grows(metric: str, values: list[float | None], exponent: float | None, config: DiagnosisConfig) -> bool:
    known = [v for v in values if v is not None]
    if exponent is None or exponent < config.growth_threshold or not known:
        return False
    spread = max(known) - min(known)
    if metric == "queries":
        return spread >= config.min_query_increase
    if metric == "compute_latency":
        return max(known) >= config.min_latency_ms
    return spread >= config.min_memory_increase_mb


def correlate_findings(
    findings: list[Finding],
    func_ids: dict[str, str],
    runtime: RuntimeReport,
    config: DiagnosisConfig,
) -> list[Finding]:
    """Return copies of ``findings`` with status and runtime evidence updated.

    ``func_ids`` maps finding_id -> the function id (``module:qualname``) the finding is in.
    """
    tracing = runtime.instrumentation.get("call_tracing", False)
    updated = []
    for finding in findings:
        finding = finding.model_copy(deep=True)
        updated.append(finding)
        metric = RULE_METRICS.get(finding.rule_id)
        func_id = func_ids.get(finding.finding_id)
        if metric is None or func_id is None:
            reason = UNMEASURABLE_REASON.get(finding.rule_id, "no runtime metric is defined for this rule")
            finding.runtime_evidence.append(RuntimeEvidence(metric="coverage", value="not measurable", description=f"Status unchanged: {reason}."))
            continue
        if metric == "queries" and not runtime.instrumentation.get("sqlalchemy_queries"):
            finding.runtime_evidence.append(RuntimeEvidence(metric="coverage", value="not measurable", description="Status unchanged: database queries were not instrumented."))
            continue

        measured = [(ep, match_runtime_endpoint(ep, runtime)) for ep in finding.reachable_endpoints]
        measured = [(ep, rt) for ep, rt in measured if rt is not None and len(rt.measurements) >= 2]
        if not measured:
            finding.runtime_evidence.append(RuntimeEvidence(metric="coverage", value="not exercised", description="Status unchanged: no measured endpoint reaches this code."))
            continue

        involved = involved_functions(finding, func_id)
        executed_any = False
        supported = False
        for endpoint_label, endpoint in measured:
            executed = any(func_id in m.function_calls_per_request for m in endpoint.measurements)
            if tracing and not executed:
                continue
            executed_any = True
            sizes, values, unit, offset = metric_series(metric, endpoint, involved)
            exponent = growth_exponent(sizes, values, offset=offset)
            grows = _grows(metric, values, exponent, config)
            supported = supported or grows
            description = (
                f"{endpoint_label}: {unit} {describe_series(sizes, values, '', 1)}; "
                f"growth {growth_class(exponent)}" + (f" (exponent {exponent:.2f})" if exponent is not None else "")
                + (". Supports the finding." if grows else ". Does not grow with input size under this workload.")
            )
            finding.runtime_evidence.append(RuntimeEvidence(
                metric=metric, value={"sizes": sizes, "values": values, "growth_exponent": exponent},
                workload=endpoint_label, description=description,
            ))
            if metric == "queries" and grows:
                top = endpoint.by_size()[-1].top_statements[:1]
                if top:
                    finding.runtime_evidence.append(RuntimeEvidence(
                        metric="repeated_statement", value=top[0].per_request, workload=endpoint_label,
                        description=f"Most repeated SQL at the largest size ({top[0].per_request:.0f}x per request): {top[0].statement[:160]}",
                    ))

        if not executed_any:
            finding.runtime_evidence.append(RuntimeEvidence(metric="coverage", value="not executed", description="Status unchanged: the workload reached the endpoint but did not execute this function."))
            continue
        finding.status = FindingStatus.CORRELATED_FINDING if supported else FindingStatus.NOT_CONFIRMED
        finding.message = finding.message.split(" Runtime confirmation is")[0] + (
            " Runtime confirmation: supported by runtime evidence under the tested workload."
            if supported else " Runtime confirmation: not confirmed under the tested workload."
        )
    return updated
