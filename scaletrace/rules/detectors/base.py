"""Detector interface and helpers shared by detectors."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

from scaletrace.analyzer.loops import LoopContext
from scaletrace.analyzer.project import FunctionInfo, ProjectIndex
from scaletrace.rules.config import DetectorConfig
from scaletrace.schemas import StaticEvidence


@dataclass
class RawMatch:
    """What a detector reports; the pipeline turns it into a Finding using rulebook metadata."""

    info: FunctionInfo
    node: ast.AST
    summary: str
    evidence: list[StaticEvidence] = field(default_factory=list)


class Detector:
    rule_id: str = ""

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        raise NotImplementedError


def loop_variables(loops: tuple[LoopContext, ...]) -> frozenset[str]:
    return frozenset().union(*(loop.target_names for loop in loops)) if loops else frozenset()


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def loop_details(loops: tuple[LoopContext, ...]) -> dict[str, object]:
    return {
        "loop_depth": len(loops),
        "innermost_loop": loops[-1].describe(),
        "enclosing_loops": [loop.describe() for loop in loops],
    }


def backticked(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)
