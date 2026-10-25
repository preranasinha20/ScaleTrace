from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scaletrace.pipeline import analyze_repository
from scaletrace.schemas import AnalysisReport

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

EXPECT_MARKER = "# expect"


def marked_lines(source: str) -> list[int]:
    """1-based line numbers carrying the ``# expect`` marker."""
    return [number for number, line in enumerate(source.splitlines(), start=1) if EXPECT_MARKER in line]


@pytest.fixture
def analyze_snippet(tmp_path: Path):
    """Write dedented ``source`` into a throwaway repo and analyze it with the given rules."""

    def run(source: str, filename: str = "app.py", rules: tuple[str, ...] = ("DB-001",)) -> tuple[AnalysisReport, str]:
        text = textwrap.dedent(source).lstrip("\n")
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return analyze_repository(tmp_path, rule_ids=list(rules)), text

    return run
