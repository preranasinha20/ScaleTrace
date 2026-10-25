"""Diagnosis models: symptoms, ranked candidates, diagnostic paths and the combined report."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scaletrace.schemas.findings import AnalysisReport

SymptomKind = Literal["query_growth", "latency_growth", "memory_growth", "errors", "user_reported"]


class Symptom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symptom_id: str
    kind: SymptomKind
    endpoint: str | None = None
    resource: str | None = Field(default=None, description="'database' for query symptoms.")
    sizes: list[int] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)
    unit: str = ""
    growth_exponent: float | None = None
    growth_class: str = "unknown"
    description: str


class CandidateScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    label: str
    file: str | None = None
    line: int | None = None
    static_score: float | None = Field(default=None, description="S(v), normalized to [0, 1].")
    dynamic_score: float | None = Field(default=None, description="D(v), normalized to [0, 1]; None = unavailable.")
    importance: float | None = Field(default=None, description="P(v), normalized to [0, 1].")
    risk: float = Field(description="Weighted combination over the available terms.")
    missing_terms: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    gnn_score: float | None = Field(default=None, description="Learned root-cause score, when a model is used.")


class DiagnosticStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    text: str
    evidence: Literal["runtime", "static", "graph", "inference", "learned"]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis_id: str
    symptom: Symptom
    supporting_symptoms: list[Symptom] = Field(default_factory=list)
    start_node: str
    status: Literal["correlated", "static_only", "unexplained"]
    approximate: bool = Field(description="True when runtime attribution or observed edges are missing.")
    ranking_method: str = "heuristic"
    path: list[str] = Field(default_factory=list)
    path_labels: list[str] = Field(default_factory=list)
    candidates: list[CandidateScore] = Field(default_factory=list)
    probable_cause: str
    steps: list[DiagnosticStep] = Field(default_factory=list)
    related_findings: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    explanation: dict[str, Any] | None = Field(default=None, description="Learned-model explanation, if any.")


class RuntimeObservation(BaseModel):
    """A runtime anomaly that no static finding on the reachable code explains."""

    model_config = ConfigDict(extra="forbid")

    symptom: Symptom
    status: Literal["runtime_observed"] = "runtime_observed"
    note: str


class DiagnosisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: Literal["diagnosis"] = "diagnosis"
    scaletrace_version: str
    repository: str
    generated_at: str
    runtime_available: bool
    scoring: dict[str, Any]
    static_report: AnalysisReport
    symptoms: list[Symptom] = Field(default_factory=list)
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    unexplained_observations: list[RuntimeObservation] = Field(default_factory=list)
    findings_by_status: dict[str, int] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
