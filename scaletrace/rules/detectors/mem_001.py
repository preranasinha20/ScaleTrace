"""MEM-001: unbounded full dataset materialization.

Reported:

* ``.all()`` / ``.fetchall()`` at the end of a database chain with no visible bound
  (``.limit()``, ``.first()``, ``fetchmany()``, ``LIMIT`` in SQL text, ...);
* ``list(<database chain>)`` with no visible bound;
* two-step reads: ``result = await db.execute(stmt)`` then ``result.scalars().all()`` (also ``result.all()``,
  ``result.unique().scalars().all()``, ``cursor.fetchall()``) with no visible bound;

A bound applied to a statement *variable* the DB call receives (``stmt = select(X).limit(10)`` then
``db.execute(stmt)``) counts as visible, for inline and two-step reads alike.
* ``await file.read()`` without a size on a parameter annotated ``UploadFile``.

"Potentially large" cannot be known statically: every unbounded read of a table is reported,
including tables that are small in practice (a documented false-positive class).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from scaletrace.analyzer.db_handles import unwind_chain
from scaletrace.analyzer.project import FunctionInfo, ProjectIndex
from scaletrace.analyzer.scopes import terminal_name
from scaletrace.rules.detectors.base import Detector, RawMatch
from scaletrace.schemas import StaticEvidence

PATTERN_ALL = ".all() on potentially large query"
PATTERN_FETCHALL = "fetchall()"
PATTERN_LIST = "list(query_or_iterator)"
PATTERN_FILE = "full file/body reads"
SQL_LIMIT = re.compile(r"\b(limit|top|fetch\s+first)\b", re.IGNORECASE)
# Methods that reshape a SQLAlchemy Result without fetching rows; allowed between the variable and .all().
RESULT_METHODS = {"scalars", "unique", "mappings", "tuples"}
_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _insert_returning(expression: ast.AST) -> str | None:
    """``insert(X).values(rows).returning(X)``: the result holds only the rows this statement wrote, so its
    size is set by the input, not by the table."""
    calls = [n for n in ast.walk(expression) if isinstance(n, ast.Call)]
    returning = any(isinstance(c.func, ast.Attribute) and c.func.attr == "returning" for c in calls)
    inserting = any((terminal_name(c.func) or "").lower().endswith("insert") for c in calls
                    if isinstance(c.func, (ast.Name, ast.Attribute)))
    return "INSERT ... RETURNING (only the rows just written)" if returning and inserting else None


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node of a scope body, not descending into nested functions or classes."""
    stack = list(getattr(scope, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, _SCOPE_BOUNDARIES):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


class FullMaterializationDetector(Detector):
    rule_id = "MEM-001"

    def _bounded(self, expression: ast.AST) -> str | None:
        bounding = set(self.config.db_bounding_methods)
        for node in ast.walk(expression):
            if isinstance(node, ast.Attribute) and node.attr in bounding:
                return f".{node.attr}()"
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_LIMIT.search(node.value):
                return "LIMIT in SQL text"
        return _insert_returning(expression)

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

    def _bounded_call(self, expression: ast.AST) -> str | None:
        """Like ``_bounded`` but only counts bounding *method calls* (``.limit(n)``), not attributes such as
        ``params.limit``. Used for expressions reached through a variable, where a stray attribute is likelier."""
        bounding = set(self.config.db_bounding_methods)
        for node in ast.walk(expression):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in bounding:
                return f".{node.func.attr}()"
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_LIMIT.search(node.value):
                return "LIMIT in SQL text"
        return _insert_returning(expression)

    @staticmethod
    def _assignments(info: FunctionInfo) -> dict[str, list[tuple[int, ast.AST]]]:
        """Local ``name = value`` assignments in this scope (not nested scopes), by name, in line order."""
        found: dict[str, list[tuple[int, ast.AST]]] = {}
        for node in _scope_nodes(info.scope.node):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                found.setdefault(node.targets[0].id, []).append((node.lineno, node.value))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                found.setdefault(node.target.id, []).append((node.lineno, node.value))
        for values in found.values():
            values.sort(key=lambda item: item[0])
        return found

    def _statement_bounded(self, call: ast.AST, assignments: dict[str, list[tuple[int, ast.AST]]], depth: int = 0,
                           seen: frozenset[str] = frozenset()) -> str | None:
        """A bound applied to a statement *variable* that a DB call receives, e.g. ``stmt = select(X).limit(10)``
        then ``db.execute(stmt)``. Every local assignment of the variable is considered (a conditional
        ``stmt = stmt.limit(n)`` counts as a visible bound, as an inline one does)."""
        if depth > 3:
            return None
        arguments: list[ast.AST] = []
        for node in ast.walk(call):
            if isinstance(node, ast.Call):
                arguments += list(node.args) + [k.value for k in node.keywords]
        for argument in arguments:
            if not isinstance(argument, ast.Name) or argument.id in seen or argument.id not in assignments:
                continue
            for _, value in assignments[argument.id]:
                bound = self._bounded_call(value) or self._statement_bounded(
                    value, assignments, depth + 1, seen | {argument.id})
                if bound:
                    return f"{bound} on `{argument.id}`"
        return None

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        fetches = set(self.config.db_unbounded_fetches)
        for info in project.iter_functions():
            uploads = self._upload_params(info)
            assignments = self._assignments(info)
            for node, _loops in project.walker.walk(info.scope.node):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func

                if isinstance(func, ast.Attribute) and func.attr in fetches:
                    pattern = PATTERN_FETCHALL if func.attr == "fetchall" else PATTERN_ALL
                    found = project.db.db_chain(node, info.db_handles)
                    if found is not None:
                        if self._bounded(node) or self._statement_bounded(node, assignments):
                            continue
                        operation, methods = found
                        yield self._match(info, node, pattern, f"`.{func.attr}()` on a database chain", operation.receiver, methods)
                        continue
                    two_step = self._two_step(project, info, node, assignments)
                    if two_step is not None:
                        yield two_step

                elif isinstance(func, ast.Name) and func.id == "list" and len(node.args) == 1:
                    inner = node.args[0]
                    found = project.db.db_chain(inner, info.db_handles)
                    if found is None or self._bounded(inner) or found[1][-1] in fetches or self._statement_bounded(inner, assignments):
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

    def _two_step(self, project: ProjectIndex, info: FunctionInfo, node: ast.Call,
                  assignments: dict[str, list[tuple[int, ast.AST]]]) -> RawMatch | None:
        """``result = await db.execute(stmt)`` ... ``result.scalars().all()``: the result object of a DB call,
        materialised on a later line. Only the latest assignment of the variable before the fetch is used, and
        only result-shaping methods (``scalars``, ``unique``, ``mappings``, ``tuples``) may sit in between."""
        base, methods = unwind_chain(node)
        if not isinstance(base, ast.Name) or not set(methods[:-1]) <= RESULT_METHODS:
            return None
        earlier = [value for line, value in assignments.get(base.id, []) if line < node.lineno]
        if not earlier:
            return None
        value = earlier[-1]
        found = project.db.db_chain(value, info.db_handles)
        fetches = set(self.config.db_unbounded_fetches)
        if found is None or set(found[1]) & fetches:
            return None
        if self._bounded(value) or self._statement_bounded(value, assignments):
            return None
        operation, source_methods = found
        source = ast.unparse(value.value if isinstance(value, ast.Await) else value)
        fetch = methods[-1]
        pattern = PATTERN_FETCHALL if fetch == "fetchall" else PATTERN_ALL
        return RawMatch(
            info=info,
            node=node,
            summary=f"`{base.id}.{'().'.join(methods)}()` materialises the result of `{operation.receiver}.{operation.method}(...)` without a visible limit",
            evidence=[
                StaticEvidence(
                    pattern=pattern,
                    description=(
                        f"`{base.id}` holds the result of `{source}` (assigned before line {node.lineno}); "
                        f"`.{fetch}()` loads every matching row into memory. No `.limit()`, pagination or streaming "
                        f"is visible in the statement or in the variables it is built from."
                    ),
                    snippet=info.ctx.snippet(node),
                    details={"receiver": base.id, "chain_methods": methods, "visible_bound": None, "form": "two_step",
                             "materialized_from": source, "db_receiver": operation.receiver,
                             "db_chain_methods": source_methods},
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
