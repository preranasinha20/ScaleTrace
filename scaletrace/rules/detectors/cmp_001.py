"""CMP-001: potentially expensive operation inside a loop.

"Expensive" is a configurable vocabulary (``expensive_functions`` / ``expensive_methods``):
sorting, parsing, (de)serialization, regex compilation, deep copies, hashing, model inference.
Whether the operation is actually costly depends on input sizes, so this is a heuristic rule.
When none of the call's arguments depend on the loop variables, the evidence notes that the
result could probably be computed once before the loop.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from scaletrace.analyzer.project import ProjectIndex, unbounded
from scaletrace.analyzer.scopes import dotted_name
from scaletrace.rules.detectors.base import Detector, RawMatch, backticked, loop_details, loop_variables, names_in
from scaletrace.schemas import StaticEvidence

PATTERN = "known expensive operation nested in loop"


class ExpensiveOperationInLoopDetector(Detector):
    rule_id = "CMP-001"

    def _operation(self, call: ast.Call) -> str | None:
        func = call.func
        dotted = dotted_name(func)
        if dotted is not None and dotted in self.config.expensive_functions:
            return dotted
        if isinstance(func, ast.Attribute) and func.attr in self.config.expensive_methods:
            return f".{func.attr}()"
        return None

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        for info in project.iter_functions():
            for node, loops in project.walker.walk(info.scope.node):
                if not isinstance(node, ast.Call) or not unbounded(loops):
                    continue
                operation = self._operation(node)
                if operation is None:
                    continue
                innermost = loops[-1]
                used_vars = sorted(loop_variables(loops) & names_in(node))
                description = f"`{operation}` runs on every iteration of `{innermost.header}` (line {innermost.line})."
                if used_vars:
                    description += f" Its inputs depend on loop variable(s) {backticked(used_vars)}."
                else:
                    description += " Its arguments do not depend on the loop variables, so the result could likely be computed once before the loop."
                yield RawMatch(
                    info=info,
                    node=node,
                    summary=f"`{operation}` inside `{innermost.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN,
                            description=description,
                            snippet=info.ctx.snippet(node),
                            details={
                                "operation": operation,
                                "loop_variables_in_arguments": used_vars,
                                "loop_invariant_arguments": not used_vars,
                                **loop_details(loops),
                            },
                        )
                    ],
                )
