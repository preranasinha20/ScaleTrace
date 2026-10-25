"""Machine-readable JSON export."""

from __future__ import annotations

import json
from pathlib import Path

from scaletrace.schemas import AnalysisReport


def write_json_report(report: AnalysisReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def report_json_schema() -> str:
    return json.dumps(AnalysisReport.model_json_schema(), indent=2)
