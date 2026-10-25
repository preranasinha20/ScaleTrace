"""Loading and validating the versioned ScaleTrace rulebook.

The rulebook JSON is a research artifact owned by the team; ScaleTrace reads it but never
modifies it. Detector tuning (which method names count as database calls, etc.) lives in
``scaletrace.rules.config`` instead.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from scaletrace.schemas import FindingStatus, Severity

BUNDLED_RULEBOOK = "rulebook_v0.1.json"


class RulebookError(ValueError):
    """The rulebook file is missing, malformed, or internally inconsistent."""


class RuleSpec(BaseModel):
    """One rule as defined in the rulebook."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    status: str
    classification: str
    category: str
    description: str
    why: str
    frameworks: list[str]
    static_evidence: list[str]
    runtime_evidence: list[str]
    false_positives: list[str]
    severity: Severity
    diagnostic_steps: list[str]
    detection: list[str]
    test_case: str
    literature_verification_required: bool


class Rulebook(BaseModel):
    """The full rulebook, validated for internal consistency."""

    model_config = ConfigDict(extra="allow")

    rulebook_version: str
    status: str
    severity_levels: dict[str, str]
    rule_statuses: dict[str, str]
    rules: list[RuleSpec]
    implementation_contract: dict[str, Any]
    finding_policy: dict[str, Any] = {}

    @model_validator(mode="after")
    def _check_consistency(self) -> Rulebook:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.status not in self.rule_statuses:
                raise ValueError(f"rule {rule.id} has unknown status {rule.status!r}")
            if rule.severity.value not in self.severity_levels:
                raise ValueError(f"rule {rule.id} has unknown severity {rule.severity.value!r}")

        declared = set(self.implementation_contract.get("status_values", []))
        supported = {status.value for status in FindingStatus}
        if declared and declared != supported:
            raise ValueError(
                f"rulebook status_values {sorted(declared)} do not match ScaleTrace's {sorted(supported)}"
            )
        return self

    def get(self, rule_id: str) -> RuleSpec:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    @property
    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self.rules]


def load_rulebook(path: str | Path | None = None) -> Rulebook:
    """Load a rulebook from ``path``, or the copy bundled with ScaleTrace when omitted."""
    try:
        if path is None:
            text = resources.files("scaletrace.rules").joinpath("data", BUNDLED_RULEBOOK).read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RulebookError(f"cannot read rulebook: {exc}") from exc

    try:
        return Rulebook.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RulebookError(f"invalid rulebook: {exc}") from exc
