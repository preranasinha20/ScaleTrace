"""Benchmark manifest and index models.

One ``scaletrace_benchmark.json`` per application records what the application is, where it came
from, which split it belongs to, and every labelled defect with its verification status. A
``benchmark_index.json`` at the benchmark root records the split assignment and, once frozen, a lock
(hashes of every test application) that later commands check.

Ground truth here is written by people. ScaleTrace's own findings are never copied into it
automatically (``scaletrace benchmark init`` only prints them as a reference), so evaluating the
rule engine against the manifest is not circular.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MANIFEST_FILE = "scaletrace_benchmark.json"
INDEX_FILE = "benchmark_index.json"
FINAL_RUNS_LOG = "final_runs.log"
SCHEMA_VERSION = "1.0"

VerificationState = Literal["unverified", "verified", "rejected"]
RuntimeVerificationState = Literal["unverified", "verified", "rejected", "not_applicable"]
Split = Literal["unassigned", "dev", "test", "excluded"]
SymptomKind = Literal["query_growth", "latency_growth", "memory_growth"]
# Two tiers, always reported separately: "source" = confirmed in the code by a person;
# "runtime" = additionally confirmed at runtime (or the rule has no runtime metric).
VERIFICATION_LEVELS = ("source", "runtime")


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    static: VerificationState = "unverified"
    runtime: RuntimeVerificationState = Field(
        default="unverified",
        description="'not_applicable' when no runtime metric exists for the rule (e.g. EXT-001 today).",
    )
    verified_by: str | None = None
    date: str | None = None
    notes: str = ""

    @property
    def fully_verified(self) -> bool:
        """Runtime tier: confirmed in the source AND at runtime (or no runtime metric exists)."""
        return self.static == "verified" and self.runtime in ("verified", "not_applicable")

    @property
    def source_confirmed(self) -> bool:
        """Source tier: a person confirmed the pattern in the code. Says nothing about runtime impact."""
        return self.static == "verified"

    def at_level(self, level: str) -> bool:
        if level == "source":
            return self.source_confirmed
        if level == "runtime":
            return self.fully_verified
        raise ValueError(f"unknown verification level {level!r}; use one of {VERIFICATION_LEVELS}")

    @property
    def rejected(self) -> bool:
        return "rejected" in (self.static, self.runtime)


class Defect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defect_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$")
    rule_id: str
    category: str
    file: str = Field(description="Path relative to the application root, POSIX separators.")
    function: str = Field(description="Qualified name as ScaleTrace reports it, e.g. 'list_orders' or 'Repo.load'.")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    endpoint: str | None = Field(default=None, description="e.g. 'GET /orders'.")
    symptom_kind: SymptomKind | None = None
    root_cause_functions: list[str] = Field(
        default_factory=list, description="Function ids 'module:qualname' where the fix belongs."
    )
    path: list[str] = Field(default_factory=list, description="Knowledge-graph node ids from symptom to resource.")
    description: str = ""
    verification: Verification = Field(default_factory=Verification)

    @model_validator(mode="after")
    def _line_order(self) -> Defect:
        if self.line_end < self.line_start:
            raise ValueError(f"{self.defect_id}: line_end {self.line_end} < line_start {self.line_start}")
        return self


class KnownNonDefect(BaseModel):
    """A rule hit the team reviewed and judged harmless (still counts as a rule false positive)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    file: str
    line: int = Field(ge=1)
    function: str | None = None
    reason: str = Field(min_length=1)


class Origin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["team", "public", "example", "generated"]
    author: str | None = None
    template_family: str | None = None
    derived_from: str | None = Field(default=None, description="app_id this application was derived from.")
    repo_url: str | None = None
    commit: str | None = None
    license: str | None = None
    controlled: bool = Field(
        default=False,
        description="A team copy of `derived_from` with a deliberately injected defect. Reported separately from "
                    "real-world apps and never counted as real-world evidence.",
    )


class RunInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload: str | None = None
    runtime_status: Literal["not_attempted", "verified", "failed"] = "not_attempted"
    notes: str = ""


class AppManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    app_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    kind: Literal["good", "bad"]
    description: str = ""
    origin: Origin
    split: Split = Field(
        default="unassigned",
        description="Mirror of the index; the index written by `scaletrace benchmark split` is authoritative.",
    )
    exclusion_reason: str | None = None
    run: RunInfo = Field(default_factory=RunInfo)
    defects: list[Defect] = Field(default_factory=list)
    known_non_defects: list[KnownNonDefect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exclusion(self) -> AppManifest:
        if self.split == "excluded" and not (self.exclusion_reason or "").strip():
            raise ValueError("split 'excluded' requires exclusion_reason")
        return self

    @property
    def is_benchmark(self) -> bool:
        """Team and public applications form the protected benchmark."""
        return self.origin.source in ("team", "public")


class IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    path: str = Field(description="Relative to the benchmark root, POSIX separators.")
    kind: Literal["good", "bad"]
    split: Split
    group: str


class LockedApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_sha256: str
    source_sha256: str


class Lock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locked_at: str
    apps: dict[str, LockedApp]


class BenchmarkIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    seed: int
    dev_fraction: float
    group_by: list[str]
    apps: list[IndexEntry] = Field(default_factory=list)
    lock: Lock | None = None

    def entry(self, app_id: str) -> IndexEntry | None:
        return next((e for e in self.apps if e.app_id == app_id), None)
