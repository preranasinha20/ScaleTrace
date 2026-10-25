"""Finding and report models.

These models are the machine-readable contract of ScaleTrace. The field names follow the
rulebook's ``implementation_contract.detector_output_required`` list so that benchmark tooling
can consume findings without knowing ScaleTrace internals.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FindingStatus(str, Enum):
    """How far the evidence for a finding goes. Never upgraded without matching evidence."""

    STATIC_CANDIDATE = "static_candidate"
    RUNTIME_OBSERVED = "runtime_observed"
    CORRELATED_FINDING = "correlated_finding"
    NOT_CONFIRMED = "not_confirmed"


class Severity(str, Enum):
    """Potential magnitude of impact if the pattern matters. Not a probability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StaticEvidence(BaseModel):
    """One piece of source-code evidence supporting a finding."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(description="The rulebook static_evidence pattern that matched.")
    description: str = Field(description="Plain-English explanation of what was found.")
    snippet: str = Field(description="Short source excerpt of the matched expression.")
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvidence(BaseModel):
    """One runtime measurement supporting (or weakening) a finding. Populated from Phase 4 on."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    value: Any
    workload: str | None = None
    description: str = ""


class Finding(BaseModel):
    """A single rule match, with the evidence that supports it."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(description="Stable identifier: rule_id:file:line:column.")
    rule_id: str
    rule_name: str
    category: str
    severity: Severity
    classification: str = Field(description="Rulebook classification, e.g. deterministic or heuristic.")
    file: str = Field(description="Path relative to the analyzed repository root (POSIX separators).")
    line: int
    end_line: int | None = None
    column: int | None = None
    function: str = Field(description="Qualified name of the enclosing function, or <module>.")
    endpoint: str | None = Field(
        default=None,
        description="Endpoint(s) whose handler is, or reaches, this function, e.g. 'GET /orders'.",
    )
    reachable_endpoints: list[str] = Field(
        default_factory=list,
        description="All endpoints that reach this function through the (approximate) call graph.",
    )
    message: str
    static_evidence: list[StaticEvidence] = Field(default_factory=list)
    runtime_evidence: list[RuntimeEvidence] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.STATIC_CANDIDATE
    possible_false_positives: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)


class SkippedFile(BaseModel):
    """A file that was discovered but could not be analyzed."""

    model_config = ConfigDict(extra="forbid")

    file: str
    reason: str


class AnalysisReport(BaseModel):
    """Result of one static analysis run over a repository."""

    model_config = ConfigDict(extra="forbid")

    report_type: Literal["static_analysis"] = "static_analysis"
    scaletrace_version: str
    rulebook_version: str
    rulebook_status: str
    generated_at: str = Field(description="UTC timestamp in ISO 8601 format.")
    repository: str = Field(description="Repository directory name (absolute paths are not recorded).")
    files_analyzed: int
    files_skipped: list[SkippedFile] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)
    rules_not_implemented: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
