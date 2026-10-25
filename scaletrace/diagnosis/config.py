"""Tunable thresholds and weights for symptom detection and candidate scoring."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DiagnosisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Risk(v) = w_s*S(v) + w_d*D(v) + w_p*P(v), renormalized over the terms that are available.
    w_static: float = 0.4
    w_dynamic: float = 0.4
    w_importance: float = 0.2

    severity_weights: dict[str, float] = Field(default={"low": 0.3, "medium": 0.6, "high": 1.0})

    # A metric "grows" when its log-log growth exponent reaches this value.
    growth_threshold: float = 0.5
    # ... and the absolute change is large enough to matter.
    min_query_increase: float = 5.0
    min_latency_ms: float = 20.0
    min_memory_increase_mb: float = 20.0
    # Exponent at which the normalized dynamic score D saturates at 1.0.
    dynamic_saturation: float = 1.0

    max_candidates: int = 5

    # Optional (off by default, so the Phase 1 heuristic is unchanged): let D(v) also credit a function
    # whose *outgoing* observed call counts grow with input size (the loop owner of an indirect N+1).
    edge_growth: bool = False
