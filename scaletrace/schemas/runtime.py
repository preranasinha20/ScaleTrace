"""Runtime measurement models produced by ``scaletrace run``.

Function identifiers use the same convention as the static analyzer: ``module:qualname``
(e.g. ``app.main:list_order_summaries``). ``<request>`` marks the request entry point.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StatementCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(description="SQL text with literals replaced by '?'.")
    per_request: float


class ObservedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caller: str
    callee: str
    calls_per_request: float


class Measurement(BaseModel):
    """Aggregated measurements for one endpoint at one input size."""

    model_config = ConfigDict(extra="forbid")

    size: int
    requests: int
    errors: int
    status_codes: dict[str, int] = Field(default_factory=dict)
    latency_ms_mean: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    throughput_rps: float | None = None
    queries_per_request: float | None = None
    query_time_ms_per_request: float | None = None
    rss_mb_peak: float | None = None
    rss_mb_delta: float | None = None
    cpu_percent_mean: float | None = None
    function_calls_per_request: dict[str, float] = Field(default_factory=dict)
    queries_by_function_per_request: dict[str, float] = Field(default_factory=dict)
    call_edges: list[ObservedEdge] = Field(default_factory=list)
    top_statements: list[StatementCount] = Field(default_factory=list)


class EndpointRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(description="Endpoint template, e.g. 'GET /orders/summary'.")
    request_name: str
    measurements: list[Measurement] = Field(default_factory=list)

    def by_size(self) -> list[Measurement]:
        return sorted(self.measurements, key=lambda m: m.size)


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: Literal["runtime"] = "runtime"
    scaletrace_version: str
    repository: str
    app: str
    generated_at: str
    status: Literal["completed", "failed"]
    error: str | None = None
    duration_s: float = 0.0
    sizes: list[int] = Field(default_factory=list)
    instrumentation: dict[str, bool] = Field(default_factory=dict)
    endpoints: list[EndpointRuntime] = Field(default_factory=list)
    unavailable_metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
