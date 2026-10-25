"""Workload specification: how to start a target app and which requests to measure.

A target repository provides ``scaletrace_workload.json``, e.g.::

    {
      "app": "app.main:app",
      "sizes": [10, 100, 1000],
      "setup": [{"method": "POST", "path": "/seed", "params": {"users": "{size}"}}],
      "requests": [{"name": "summary", "method": "GET", "path": "/orders/summary"}],
      "repetitions": 5
    }

``{size}`` placeholders in paths, params and JSON bodies are replaced by the current input size.
Requests always go to 127.0.0.1 on a port chosen by ScaleTrace; there is no host setting, so a
workload can never be pointed at a remote or production system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

WORKLOAD_FILE = "scaletrace_workload.json"
SIZE_PLACEHOLDER = "{size}"


class WorkloadError(ValueError):
    """The workload file is missing or invalid."""


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    method: str = "GET"
    path: str
    path_params: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None
    endpoint: str | None = Field(default=None, description="Static endpoint label; default 'METHOD path'.")

    @field_validator("method")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        if not value.startswith("/") or "://" in value:
            raise ValueError("path must be an absolute path on the target app, e.g. /orders")
        return value

    @property
    def label(self) -> str:
        return self.endpoint or f"{self.method} {self.path}"

    @property
    def display_name(self) -> str:
        return self.name or self.label


class WorkloadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str = Field(description="ASGI app as module:attribute, importable from working_dir.")
    working_dir: str = Field(default=".", description="Relative to the repository root.")
    python: str | None = Field(default=None, description="Interpreter for the target; default: ScaleTrace's own.")
    sizes: list[int] = Field(default=[10, 100, 1000], min_length=2)
    setup: list[RequestSpec] = Field(default_factory=list, description="Run before measuring each size.")
    requests: list[RequestSpec] = Field(min_length=1)
    repetitions: int = Field(default=5, ge=1)
    warmup: int = Field(default=1, ge=0)
    concurrency: int = Field(default=1, ge=1, le=64)
    trace_calls: bool = True
    startup_timeout_s: float = 30.0
    request_timeout_s: float = 60.0
    total_timeout_s: float = 900.0


def substitute(value: Any, size: int) -> Any:
    """Replace ``{size}`` placeholders (exact match -> int, embedded -> string)."""
    if isinstance(value, str):
        if value == SIZE_PLACEHOLDER:
            return size
        return value.replace(SIZE_PLACEHOLDER, str(size))
    if isinstance(value, dict):
        return {k: substitute(v, size) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, size) for v in value]
    return value


def render_path(spec: RequestSpec, size: int) -> str:
    path = substitute(spec.path, size)
    for key, value in spec.path_params.items():
        path = path.replace("{" + key + "}", str(substitute(value, size)))
    return path


def load_workload(path: str | Path) -> WorkloadSpec:
    try:
        return WorkloadSpec.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except OSError as exc:
        raise WorkloadError(f"cannot read workload: {exc}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise WorkloadError(f"invalid workload: {exc}") from exc
