"""Turning runtime measurements into symptoms: metrics that grow with input size."""

from __future__ import annotations

from scaletrace.diagnosis.config import DiagnosisConfig
from scaletrace.runtime.analysis import describe_series, growth_class, growth_exponent
from scaletrace.schemas.diagnosis import Symptom
from scaletrace.schemas.runtime import EndpointRuntime, RuntimeReport

SYMPTOM_PRIORITY = ["query_growth", "memory_growth", "latency_growth", "errors", "user_reported"]


def _series(endpoint: EndpointRuntime, attribute: str) -> tuple[list[int], list[float | None]]:
    measurements = endpoint.by_size()
    return [m.size for m in measurements], [getattr(m, attribute) for m in measurements]


def compute_latency_series(endpoint: EndpointRuntime) -> tuple[list[int], list[float | None]]:
    """Mean latency minus mean time spent in SQL: a rough proxy for in-process work."""
    measurements = endpoint.by_size()
    values: list[float | None] = []
    for m in measurements:
        if m.latency_ms_mean is None:
            values.append(None)
        else:
            values.append(max(0.0, m.latency_ms_mean - (m.query_time_ms_per_request or 0.0)))
    return [m.size for m in measurements], values


def detect_symptoms(runtime: RuntimeReport, config: DiagnosisConfig) -> list[Symptom]:
    symptoms: list[Symptom] = []
    for endpoint in runtime.endpoints:
        if len(endpoint.measurements) < 2:
            continue
        name = endpoint.endpoint

        sizes, queries = _series(endpoint, "queries_per_request")
        exponent = growth_exponent(sizes, queries, offset=1.0)
        known = [q for q in queries if q is not None]
        if exponent is not None and exponent >= config.growth_threshold and known and max(known) - min(known) >= config.min_query_increase:
            symptoms.append(Symptom(
                symptom_id=f"query_growth:{name}", kind="query_growth", endpoint=name, resource="database",
                sizes=sizes, values=queries, unit="queries/request", growth_exponent=exponent, growth_class=growth_class(exponent),
                description=f"{name}: SQL queries per request grow {growth_class(exponent)}ly with input size "
                f"({describe_series(sizes, queries, ' q', 0)}; growth exponent {exponent:.2f}).",
            ))

        sizes, latency = _series(endpoint, "latency_ms_p95")
        exponent = growth_exponent(sizes, latency)
        known = [v for v in latency if v is not None]
        if exponent is not None and exponent >= config.growth_threshold and known and max(known) >= config.min_latency_ms:
            symptoms.append(Symptom(
                symptom_id=f"latency_growth:{name}", kind="latency_growth", endpoint=name,
                sizes=sizes, values=latency, unit="ms (p95)", growth_exponent=exponent, growth_class=growth_class(exponent),
                description=f"{name}: P95 latency grows {growth_class(exponent)}ly with input size "
                f"({describe_series(sizes, latency, ' ms')}; growth exponent {exponent:.2f}).",
            ))

        sizes, memory = _series(endpoint, "rss_mb_peak")
        known = [v for v in memory if v is not None]
        exponent = growth_exponent(sizes, memory)
        if exponent is not None and known and max(known) - min(known) >= config.min_memory_increase_mb and exponent > 0:
            symptoms.append(Symptom(
                symptom_id=f"memory_growth:{name}", kind="memory_growth", endpoint=name,
                sizes=sizes, values=memory, unit="MB (process RSS peak)", growth_exponent=exponent, growth_class=growth_class(exponent),
                description=f"{name}: process memory peak grows with input size ({describe_series(sizes, memory, ' MB')}).",
            ))

        errors = [m.errors for m in endpoint.by_size()]
        if any(errors):
            symptoms.append(Symptom(
                symptom_id=f"errors:{name}", kind="errors", endpoint=name, sizes=[m.size for m in endpoint.by_size()],
                values=[float(e) for e in errors], unit="failed requests",
                description=f"{name}: failed requests at some input sizes ({describe_series([m.size for m in endpoint.by_size()], errors, '', 0)}).",
            ))
    return symptoms


def group_by_endpoint(symptoms: list[Symptom]) -> list[tuple[Symptom, list[Symptom]]]:
    """One (primary, supporting) group per endpoint, primary chosen by SYMPTOM_PRIORITY."""
    groups: dict[str | None, list[Symptom]] = {}
    for symptom in symptoms:
        groups.setdefault(symptom.endpoint, []).append(symptom)
    result = []
    for members in groups.values():
        members.sort(key=lambda s: SYMPTOM_PRIORITY.index(s.kind))
        result.append((members[0], members[1:]))
    return result
