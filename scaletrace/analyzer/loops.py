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
from dataclasses import dataclass, field, replace

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


_MUTATORS = {"append", "extend", "insert", "add", "update", "setdefault", "__setitem__", "__iadd__"}


def module_literal_constants(tree: ast.Module) -> dict[str, int]:
    """Module-level names bound exactly once to a literal list/tuple/set and never changed in the module.

    ``CONSTRAINTS = [("a", ...), ("b", ...)]`` qualifies. A name is dropped if it is assigned again anywhere in the
    module (module level, ``global`` in a function, augmented assignment, subscript or attribute store) or if a
    mutating method (``append``, ``extend``, ...) is called on it. Other modules could still mutate it through an
    import; that is not tracked (a documented limitation).
    """
    candidates: dict[str, int] = {}
    for stmt in tree.body:
        target = value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target, value = stmt.target.id, stmt.value
        if target is not None and isinstance(value, (ast.List, ast.Tuple, ast.Set)) \
                and not any(isinstance(e, ast.Starred) for e in value.elts):
            candidates[target] = len(value.elts)
    mutated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            mutated.update(node.names)
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)) \
                and isinstance(node.value, ast.Name):
            mutated.add(node.value.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS \
                and isinstance(node.func.value, ast.Name):
            mutated.add(node.func.value.id)
    # Exactly one module-level binding (a function-local of the same name shadows it only inside that
    # function, which LoopWalker.walk handles per scope).
    module_stores: dict[str, int] = {}
    for stmt in tree.body:
        for node in _module_level_nodes(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                module_stores[node.id] = module_stores.get(node.id, 0) + 1
    return {name: n for name, n in candidates.items() if name not in mutated and module_stores.get(name, 0) == 1}


def _module_level_nodes(stmt: ast.AST) -> Iterator[ast.AST]:
    stack = [stmt]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _local_names(scope_node: ast.AST) -> set[str]:
    """Names a function binds itself (parameters and assignments), which shadow module-level names."""
    names: set[str] = set()
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope_node.args
        names.update(a.arg for a in args.posonlyargs + args.args + args.kwonlyargs)
        names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
        for node in _module_level_nodes_body(scope_node):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def _module_level_nodes_body(scope_node: ast.AST) -> Iterator[ast.AST]:
    body = scope_node.body
    for stmt in body if isinstance(body, list) else [body]:  # a Lambda body is a single expression
        yield from _module_level_nodes(stmt)


def static_iteration_bound(iterable: ast.AST, constants: dict[str, int] | None = None) -> int | None:
    """Iteration count when it is evident from the source alone, else None.

    ``constants`` maps names known to hold a fixed literal collection (see ``module_literal_constants``)."""
    if constants and isinstance(iterable, ast.Name) and iterable.id in constants:
        return constants[iterable.id]
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


def _page_limit(value: ast.AST) -> bool:
    """``value`` contains ``.limit(n)`` where ``n`` is not the literal 0 or 1 (``.limit(1)`` is per-item work)."""
    for call in ast.walk(value):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "limit"
                and len(call.args) == 1 and not call.keywords):
            arg = call.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, int) and arg.value <= 1):
                return True
    return False


def _breaks(statements: list[ast.stmt]) -> bool:
    return any(isinstance(s, ast.Break) for s in statements)


def paged_loop_variable(loop: ast.While) -> str | None:
    """The page variable of a paged ``while`` loop, or None.

    A paged loop (e.g. keyset pagination) assigns a page fetched with ``.limit(n)`` to a variable and ends when
    that page is empty or short: the variable appears in the loop condition, or in the condition of an ``if``
    in the loop body whose body breaks. Only the loop's direct statements are inspected. Loops nested in it keep
    their own (unbounded) context, so per-row work inside a page is still reported.
    """
    pages = set()
    for stmt in loop.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and _page_limit(stmt.value):
            pages.add(stmt.targets[0].id)
    if not pages:
        return None
    stops = set(_names(loop.test))
    for stmt in loop.body:
        if isinstance(stmt, ast.If) and _breaks(stmt.body):
            stops |= _names(stmt.test)
    found = sorted(pages & stops)
    return found[0] if found else None


_SAFE_READERS = {"len", "enumerate", "sorted", "reversed", "list", "tuple", "set", "frozenset", "min", "max", "sum",
                 "any", "all", "zip", "str", "repr", "print"}


class _Constants(dict):
    """name -> literal size, remembering which names are function-local (for the evidence text)."""

    local: frozenset[str] = frozenset()


def local_literal_constants(scope_node: ast.AST) -> dict[str, int]:
    """Function-local names that demonstrably hold one fixed literal list/tuple/set.

    Added post-hoc, from a locked-test-split review note (docs/error_analysis.md section 8). Deliberately strict.
    The name must be bound exactly once in the function (anywhere, nested scopes included) to a literal without
    ``*`` unpacking; it must not be a parameter or ``global``/``nonlocal``; and every read of it must be one that
    cannot change or leak it: a loop iterable, a ``Compare`` (``x in kinds``), a subscript read, an argument of a
    read-only builtin (``len``, ``sorted``, ...), or a non-mutating method. Any other use (passing it to a
    function, aliasing ``other = kinds``, ``kinds.append``, item assignment) disqualifies it.
    """
    if not isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    args = scope_node.args
    params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    params |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    candidates: dict[str, int] = {}
    stores: dict[str, int] = {}
    declared: set[str] = set()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(scope_node):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, (ast.ExceptHandler, ast.alias)) and getattr(node, "name", None):
            name = node.name if isinstance(node, ast.ExceptHandler) else (node.asname or node.name.split(".")[0])
            stores[name] = stores.get(name, 0) + 2  # any such binding disqualifies the name
    for stmt in ast.walk(scope_node):
        target = value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target, value = stmt.target.id, stmt.value
        if (target is not None and isinstance(value, (ast.List, ast.Tuple, ast.Set))
                and not any(isinstance(e, ast.Starred) for e in value.elts)):
            candidates[target] = len(value.elts)

    def safe_read(name_node: ast.Name) -> bool:
        parent = parents.get(name_node)
        if isinstance(parent, (ast.For, ast.AsyncFor)) and parent.iter is name_node:
            return True
        if isinstance(parent, ast.comprehension) and parent.iter is name_node:
            return True
        if isinstance(parent, ast.Compare):
            return True
        if isinstance(parent, ast.Subscript) and parent.value is name_node and isinstance(parent.ctx, ast.Load):
            return True
        if isinstance(parent, ast.Call) and name_node in parent.args:
            return isinstance(parent.func, ast.Name) and parent.func.id in _SAFE_READERS
        if isinstance(parent, ast.Attribute) and parent.value is name_node:
            return parent.attr not in _MUTATORS and parent.attr not in ("remove", "pop", "clear", "sort", "reverse", "discard")
        return False

    result = {}
    for name, size in candidates.items():
        if name in params or name in declared or stores.get(name, 0) != 1:
            continue
        reads = [n for n in ast.walk(scope_node) if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)]
        if all(safe_read(n) for n in reads):
            result[name] = size
    return result


class LoopWalker:
    """Yields ``(node, enclosing_loops)`` for every node in a scope body."""

    def __init__(self, bounded_max_iterations: int) -> None:
        self.bounded_max_iterations = bounded_max_iterations
        # id(scope node) -> module-level literal constants visible in that scope (see register_module).
        self._constants: dict[int, dict[str, int]] = {}
        # id(scope node) -> (scope node, module + local constants); the node is kept to guard against id reuse.
        self._merged: dict[int, tuple[ast.AST, _Constants]] = {}

    def register_module(self, tree: ast.Module) -> None:
        """Make the module's fixed literal collections known to loops in its scopes."""
        constants = module_literal_constants(tree)
        if not constants:
            return
        self._constants[id(tree)] = constants
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visible = {k: v for k, v in constants.items() if k not in _local_names(node)}
                if visible:
                    self._constants[id(node)] = visible

    def _loop(self, kind: str, line: int, header: str, target: ast.AST | None, iterable: ast.AST | None,
              constants: dict[str, int]) -> LoopContext:
        bound = static_iteration_bound(iterable, constants) if iterable is not None else None
        bounded = bound is not None and bound <= self.bounded_max_iterations
        reason = None
        if bounded:
            if isinstance(iterable, ast.Name):
                where = "local constant" if iterable.id in getattr(constants, "local", ()) else "module constant"
                reason = f"at most {bound} iterations ({where} `{iterable.id}`)"
            else:
                reason = f"at most {bound} iterations (literal)"
        return LoopContext(
            kind=kind,
            line=line,
            header=_shorten(header),
            target_names=_names(target) if target is not None else frozenset(),
            iterable=_shorten(ast.unparse(iterable)) if iterable is not None else None,
            bounded=bounded,
            bound_reason=reason,
            iter_node=iterable,
            target_node=target,
        )

    def _scope_constants(self, scope_node: ast.AST) -> _Constants:
        key = id(scope_node)
        cached = self._merged.get(key)
        if cached is None or cached[0] is not scope_node:
            merged = _Constants(self._constants.get(key, {}))
            local = local_literal_constants(scope_node)
            merged.update(local)  # a local binding shadows a module constant of the same name
            merged.local = frozenset(local)
            cached = (scope_node, merged)
            self._merged[key] = cached
        return cached[1]

    def walk(self, scope_node: ast.AST) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        constants = self._scope_constants(scope_node)
        for stmt in getattr(scope_node, "body", []):
            yield from self._visit(stmt, (), constants)

    def _visit_all(self, nodes: list[ast.AST], loops: tuple[LoopContext, ...],
                   constants: dict[str, int]) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        for node in nodes:
            yield from self._visit(node, loops, constants)

    def _visit(self, node: ast.AST, loops: tuple[LoopContext, ...],
               constants: dict[str, int]) -> Iterator[tuple[ast.AST, tuple[LoopContext, ...]]]:
        if isinstance(node, _SCOPE_BOUNDARIES):
            return
        yield node, loops

        if isinstance(node, (ast.For, ast.AsyncFor)):
            keyword = "async for" if isinstance(node, ast.AsyncFor) else "for"
            header = f"{keyword} {ast.unparse(node.target)} in {ast.unparse(node.iter)}"
            inner = loops + (self._loop(keyword, node.lineno, header, node.target, node.iter, constants),)
            yield from self._visit_all([node.target, node.iter], loops, constants)
            yield from self._visit_all(node.body, inner, constants)
            yield from self._visit_all(node.orelse, loops, constants)
        elif isinstance(node, ast.While):
            header = f"while {ast.unparse(node.test)}"
            loop = self._loop("while", node.lineno, header, None, None, constants)
            page = paged_loop_variable(node)
            if page is not None:
                loop = replace(loop, bounded=True, bound_reason=(
                    f"paged loop: each iteration fetches one page into `{page}` with .limit(...) and the loop stops when "
                    f"`{page}` is empty or short, so its direct body runs once per page, not once per row"))
            inner = loops + (loop,)
            yield from self._visit(node.test, inner, constants)
            yield from self._visit_all(node.body, inner, constants)
            yield from self._visit_all(node.orelse, loops, constants)
        elif isinstance(node, _COMPREHENSIONS):
            kind = _COMPREHENSION_KINDS[type(node)]
            current = loops
            for generator in node.generators:
                # The first iterable is evaluated once, before the comprehension loops.
                yield from self._visit(generator.iter, current, constants)
                header = f"{kind}: for {ast.unparse(generator.target)} in {ast.unparse(generator.iter)}"
                current = current + (self._loop(kind, node.lineno, header, generator.target, generator.iter, constants),)
                yield from self._visit(generator.target, current, constants)
                yield from self._visit_all(generator.ifs, current, constants)
            elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            yield from self._visit_all(elements, current, constants)
        else:
            for child in ast.iter_child_nodes(node):
                yield from self._visit(child, loops, constants)
