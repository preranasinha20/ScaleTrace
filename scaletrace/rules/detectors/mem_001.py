"""MEM-001: unbounded full dataset materialization.

Reported:

* ``.all()`` / ``.fetchall()`` at the end of a database chain with no visible bound
  (``.limit()``, ``.first()``, ``fetchmany()``, ``LIMIT`` in SQL text, ...);
* ``list(<database chain>)`` with no visible bound;
* ``await file.read()`` without a size on a parameter annotated ``UploadFile``.

"Potentially large" cannot be known statically: every unbounded read of a table is reported,
including tables that are small in practice (a documented false-positive class).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from scaletrace.analyzer.project import FunctionInfo, ProjectIndex
from scaletrace.analyzer.scopes import terminal_name
from scaletrace.rules.detectors.base import Detector, RawMatch
from scaletrace.schemas import StaticEvidence

PATTERN_ALL = ".all() on potentially large query"
PATTERN_FETCHALL = "fetchall()"
PATTERN_LIST = "list(query_or_iterator)"
PATTERN_FILE = "full file/body reads"
SQL_LIMIT = re.compile(r"\b(limit|top|fetch\s+first)\b", re.IGNORECASE)


class FullMaterializationDetector(Detector):
    rule_id = "MEM-001"

    def _bounded(self, expression: ast.AST) -> str | None:
        bounding = set(self.config.db_bounding_methods)
        for node in ast.walk(expression):
            if isinstance(node, ast.Attribute) and node.attr in bounding:
                return f".{node.attr}()"
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_LIMIT.search(node.value):
                return "LIMIT in SQL text"
        return None

    def _upload_params(self, info: FunctionInfo) -> set[str]:
        node = info.scope.node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return set()
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        return {
            a.arg for a in args
            if a.annotation is not None and any(
                terminal_name(n) == "UploadFile" for n in ast.walk(a.annotation) if isinstance(n, (ast.Name, ast.Attribute))
            )
        }

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        fetches = set(self.config.db_unbounded_fetches)
        for info in project.iter_functions():
            uploads = self._upload_params(info)
            for node, _loops in project.walker.walk(info.scope.node):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func

                if isinstance(func, ast.Attribute) and func.attr in fetches:
                    found = project.db.db_chain(node, info.db_handles)
                    if found is None or self._bounded(node):
                        continue
                    operation, methods = found
                    pattern = PATTERN_FETCHALL if func.attr == "fetchall" else PATTERN_ALL
                    yield self._match(info, node, pattern, f"`.{func.attr}()` on a database chain", operation.receiver, methods)

                elif isinstance(func, ast.Name) and func.id == "list" and len(node.args) == 1:
                    inner = node.args[0]
                    found = project.db.db_chain(inner, info.db_handles)
                    if found is None or self._bounded(inner) or found[1][-1] in fetches:
                        continue
                    operation, methods = found
                    yield self._match(info, node, PATTERN_LIST, "`list(...)` over a database chain", operation.receiver, methods)

                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "read"
                    and not node.args
                    and isinstance(func.value, ast.Name)
                    and func.value.id in uploads
                ):
                    yield RawMatch(
                        info=info,
                        node=node,
                        summary=f"`{func.value.id}.read()` reads a whole upload into memory",
                        evidence=[
                            StaticEvidence(
                                pattern=PATTERN_FILE,
                                description=f"`{func.value.id}.read()` loads the entire uploaded file into memory; no size limit or streaming is visible.",
                                snippet=info.ctx.snippet(node),
                                details={"source": "UploadFile"},
                            )
                        ],
                    )

    def _match(self, info: FunctionInfo, node: ast.Call, pattern: str, what: str, receiver: str, methods: list[str]) -> RawMatch:
        return RawMatch(
            info=info,
            node=node,
            summary=f"{what} without a visible limit",
            evidence=[
                StaticEvidence(
                    pattern=pattern,
                    description=(
                        f"{what} (`{receiver}.{'(...).'.join(methods)}(...)`) loads every matching row into "
                        f"memory; no `.limit()`, pagination or streaming is visible in the chain."
                    ),
                    snippet=info.ctx.snippet(node),
                    details={"receiver": receiver, "chain_methods": methods, "visible_bound": None},
                )
            ],
        )
