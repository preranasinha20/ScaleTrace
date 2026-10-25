"""Walking a scope while tracking which loops enclose each node.

Only code that is re-executed per iteration counts as "inside" a loop:

* ``for`` / ``async for``: the body (not the iterable, not the ``else`` block);
* ``while``: the condition and the body (not the ``else`` block);
* comprehensions: the element, the conditions, and every iterable after the first.

Nested function, lambda and class bodies are not entered: defining a function in a loop does
not run its body. Those bodies are analyzed as their own scopes.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

LOOP_HEADER_MAX_CHARS = 100

_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_COMPREHENSION_KINDS = {
    ast.ListComp: "list comprehension",
    ast.SetComp: "set comprehension",
    ast.GeneratorExp: "generator expression",
    ast.DictComp: "dict comprehension",
}


@dataclass(frozen=True)
class LoopContext:
    kind: str
    line: int
    header: str
    target_names: frozenset[str]
    iterable: str | None
    bounded: bool
    bound_reason: str | None = None
    iter_node: ast.AST | None = field(default=None, compare=False, hash=False, repr=False)
    target_node: ast.AST | None = field(default=None, compare=False, hash=False, repr=False)

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "line": self.line,
            "header": self.header,
            "iterable": self.iterable,
            "bounded": self.bounded,
        }


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LOOP_HEADER_MAX_CHARS else text[: LOOP_HEADER_MAX_CHARS - 3] + "..."


def _names(node: ast.AST) -> frozenset[str]:
    return frozenset(n.id for n in ast.walk(node) if isinstance(n, ast.Name))


def static_iteration_bound(iterable: ast.AST) -> int | None:
    """Iteration count when it is evident from the source alone, else None."""
    if isinstance(iterable, (ast.List, ast.Tuple, ast.Set)):
        if any(isinstance(elt, ast.Starred) for elt in iterable.elts):
            return None
        return len(iterable.elts)
    if (
        isinstance(iterable, ast.Call)
        and isinstance(iterable.func, ast.Name)
        and iterable.func.id == "range"
        and not iterable.keywords
        and 1 <= len(iterable.args) <= 3
        and all(isinstance(arg, ast.Constant) and type(arg.value) is int for arg in iterable.args)
    ):
        try:
            return len(range(*(arg.value for arg in iterable.args)))  # type: ignore[union-attr]
        except ValueError:  # range() step of zero
            return None
    return None


class LoopWalker:
    """Yields ``(node, enclosing_loops)`` for every node in a scope body."""

    def __init__(self, bounded_max_iterations: int) -> None:
        self.bounded_max_iterations = bounded_max_iterations

    def _loop(self, kind: str, line: int, header: str, target: ast.AST | None, iterable: ast.AST | None) -> LoopContext:
        bound = static_iteration_bound(iterable) if iterable is not None else None
        bounded = bound is not None and bound <= self.bounded_max_iterations
        return LoopContext(
            kind=kind,
            line=line,
            header=_shorten(header),
            target_names=_names(target) if target is not None else frozenset(),
            iterable=_shorten(ast.unparse(iterable)) if iterable is not None else None,
            bounded=bounded,
            bound_reason=f"at most {bound} iterations (literal)" if bounded else None,
            iter_node=iterable,
            target_node=target,
        )

    def walk(self, scope_node: ast.AST) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        for stmt in getattr(scope_node, "body", []):
            yield from self._visit(stmt, ())

    def _visit_all(self, nodes: list[ast.AST], loops: tuple[LoopContext, ...]) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        for node in nodes:
            yield from self._visit(node, loops)

    def _visit(self, node: ast.AST, loops: tuple[LoopContext, ...]) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        if isinstance(node, _SCOPE_BOUNDARIES):
            return
        yield node, loops

        if isinstance(node, (ast.For, ast.AsyncFor)):
            keyword = "async for" if isinstance(node, ast.AsyncFor) else "for"
            header = f"{keyword} {ast.unparse(node.target)} in {ast.unparse(node.iter)}"
            inner = loops + (self._loop(keyword, node.lineno, header, node.target, node.iter),)
            yield from self._visit_all([node.target, node.iter], loops)
            yield from self._visit_all(node.body, inner)
            yield from self._visit_all(node.orelse, loops)
        elif isinstance(node, ast.While):
            header = f"while {ast.unparse(node.test)}"
            inner = loops + (self._loop("while", node.lineno, header, None, None),)
            yield from self._visit(node.test, inner)
            yield from self._visit_all(node.body, inner)
            yield from self._visit_all(node.orelse, loops)
        elif isinstance(node, _COMPREHENSIONS):
            kind = _COMPREHENSION_KINDS[type(node)]
            current = loops
            for generator in node.generators:
                # The first iterable is evaluated once, before the comprehension loops.
                yield from self._visit(generator.iter, current)
                header = f"{kind}: for {ast.unparse(generator.target)} in {ast.unparse(generator.iter)}"
                current = current + (self._loop(kind, node.lineno, header, generator.target, generator.iter),)
                yield from self._visit(generator.target, current)
                yield from self._visit_all(generator.ifs, current)
            elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            yield from self._visit_all(elements, current)
        else:
            for child in ast.iter_child_nodes(node):
                yield from self._visit(child, loops)
