"""DB-003: ORM relationship N+1.

1. Collect relationship attributes declared in the project's ORM models
   (``relationship(...)`` / SQLModel ``Relationship(...)``, including ``backref=``), with their
   ``lazy=`` setting. Eager settings (``selectin``, ``joined``, ...) are excluded.
2. Report access to such an attribute on a loop variable inside an unbounded loop, when the
   loop iterates over query results: a database chain, a local name assigned from one, another
   relationship attribute, or the result of a project function that reaches the database.
3. Skip when the query that produced the collection visibly requests eager loading of that
   attribute (``.options(selectinload(User.orders))``).

Heuristic: attributes are matched by name, because the model class of a loop variable is not
known statically.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from scaletrace.analyzer.loops import LoopContext
from scaletrace.analyzer.project import FunctionInfo, ProjectIndex, unbounded
from scaletrace.analyzer.scopes import terminal_name
from scaletrace.rules.detectors.base import Detector, RawMatch
from scaletrace.schemas import StaticEvidence

PATTERN = "relationship attribute access inside loop"
RELATIONSHIP_FACTORIES = {"relationship", "Relationship"}


@dataclass(frozen=True)
class RelationshipAttr:
    model: str
    attr: str
    lazy: str
    line: int
    file: str


def _lazy_value(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg == "lazy" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
        if keyword.arg == "sa_relationship_kwargs" and isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values):
                if isinstance(key, ast.Constant) and key.value == "lazy" and isinstance(value, ast.Constant):
                    return str(value.value)
    return "select"


def collect_relationships(project: ProjectIndex) -> dict[str, list[RelationshipAttr]]:
    found: dict[str, list[RelationshipAttr]] = {}
    for ctx in project.modules.values():
        for cls in (n for n in ast.walk(ctx.tree) if isinstance(n, ast.ClassDef)):
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                if not isinstance(value, ast.Call) or terminal_name(value.func) not in RELATIONSHIP_FACTORIES:
                    continue
                lazy = _lazy_value(value)
                for target in targets:
                    if isinstance(target, ast.Name):
                        found.setdefault(target.id, []).append(RelationshipAttr(cls.name, target.id, lazy, stmt.lineno, ctx.rel_path))
                for keyword in value.keywords:
                    if keyword.arg == "backref" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        name = keyword.value.value
                        found.setdefault(name, []).append(RelationshipAttr(f"(backref of {cls.name})", name, "select", stmt.lineno, ctx.rel_path))
    return found


class OrmNPlusOneDetector(Detector):
    rule_id = "DB-003"

    def _query_sources(self, info: FunctionInfo, project: ProjectIndex) -> dict[str, ast.AST]:
        """Local names assigned from database chains or from functions reaching the database."""
        sources: dict[str, ast.AST] = {}
        for node, _ in project.walker.walk(info.scope.node):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if self._is_query_result(node.value, info, project, sources):
                    sources[node.targets[0].id] = node.value
        return sources

    def _is_query_result(self, expr: ast.AST, info: FunctionInfo, project: ProjectIndex, sources: dict[str, ast.AST]) -> bool:
        if isinstance(expr, ast.Await):
            expr = expr.value
        if project.db.db_chain(expr, info.db_handles) is not None:
            return True
        if isinstance(expr, ast.Name) and expr.id in sources:
            return True
        if isinstance(expr, ast.Call):
            callee = project.resolve_callable(info, expr.func)
            if callee is not None and project.resource_path(callee, "database") is not None:
                return True
        return False

    def _eager_loaded(self, expression: ast.AST | None, attr: str) -> bool:
        if expression is None:
            return False
        options = set(self.config.eager_loading_options)
        for node in ast.walk(expression):
            if isinstance(node, ast.Call) and terminal_name(node.func) in options:
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Attribute) and arg.attr == attr:
                        return True
                    if isinstance(arg, ast.Constant) and arg.value == attr:
                        return True
        return False

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        relationships = collect_relationships(project)
        eager = set(self.config.eager_lazy_values)
        lazy_attrs = {name: rels for name, rels in relationships.items() if any(r.lazy not in eager for r in rels)}
        if not lazy_attrs:
            return
        for info in project.iter_functions():
            sources = self._query_sources(info, project)
            reported: set[tuple[int, str, str]] = set()
            for node, loops in project.walker.walk(info.scope.node):
                if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                    continue
                if node.attr not in lazy_attrs or not isinstance(node.value, ast.Name) or not unbounded(loops):
                    continue
                loop = self._loop_over(node.value.id, loops)
                if loop is None:
                    continue
                provenance = self._provenance(loop, info, project, sources, lazy_attrs)
                if provenance is None:
                    continue
                origin = sources.get(loop.iter_node.id) if isinstance(loop.iter_node, ast.Name) else loop.iter_node
                if self._eager_loaded(origin, node.attr):
                    continue
                key = (loop.line, node.value.id, node.attr)
                if key in reported:
                    continue
                reported.add(key)
                rel = next(r for r in lazy_attrs[node.attr] if r.lazy not in eager)
                yield RawMatch(
                    info=info,
                    node=node,
                    summary=f"lazy relationship `{node.value.id}.{node.attr}` accessed inside `{loop.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN,
                            description=(
                                f"`{node.value.id}.{node.attr}` is accessed for every element of `{loop.header}` "
                                f"(line {loop.line}). `{node.attr}` is declared as a relationship on {rel.model} "
                                f"({rel.file}:{rel.line}) with lazy='{rel.lazy}', and the loop iterates over "
                                f"{provenance}; each access may issue one query per parent."
                            ),
                            snippet=info.ctx.snippet(node),
                            details={
                                "relationship": node.attr,
                                "declared_on": rel.model,
                                "declared_at": f"{rel.file}:{rel.line}",
                                "lazy": rel.lazy,
                                "iterable_provenance": provenance,
                                "loop": loop.describe(),
                            },
                        )
                    ],
                )

    @staticmethod
    def _loop_over(name: str, loops: tuple[LoopContext, ...]) -> LoopContext | None:
        for loop in reversed(loops):
            if name in loop.target_names:
                return loop
        return None

    def _provenance(self, loop: LoopContext, info: FunctionInfo, project: ProjectIndex, sources, lazy_attrs) -> str | None:
        iterable = loop.iter_node
        if iterable is None:
            return None
        if project.db.db_chain(iterable, info.db_handles) is not None:
            return "a database query"
        if isinstance(iterable, ast.Name) and iterable.id in sources:
            return f"`{iterable.id}`, assigned from a database query"
        if isinstance(iterable, ast.Attribute) and iterable.attr in lazy_attrs:
            return f"the relationship `{ast.unparse(iterable)}`"
        if self._is_query_result(iterable, info, project, sources):
            return "the result of a function that queries the database"
        return None
