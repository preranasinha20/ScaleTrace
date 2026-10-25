"""Inferring which names refer to database handles (sessions, connections, cursors).

Python has no static types to rely on, so this is evidence-based guessing. Each inference
records its *basis* so reports can show how strong the evidence is:

* ``annotation``  -- parameter/variable annotated as e.g. ``Session``;
* ``dependency``  -- FastAPI parameter ``= Depends(get_db)``;
* ``factory``     -- assigned from e.g. ``SessionLocal()`` or ``engine.connect()``;
* ``module_factory`` -- the same, at module level;
* ``name_heuristic`` -- only the variable name suggests a DB handle (weakest).

Explicit evidence that a name is an HTTP client (``requests.Session()``) overrides the name
heuristic, so ``session.get(url)`` on a requests session is not mistaken for a DB call.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from scaletrace.analyzer.scopes import FunctionScope, dotted_name, terminal_name
from scaletrace.rules.config import DetectorConfig


@dataclass(frozen=True)
class HandleInfo:
    is_db: bool
    basis: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.basis}: {self.detail}" if self.detail else self.basis


@dataclass(frozen=True)
class DbOperation:
    method: str
    receiver: str
    handle: HandleInfo


class DbHandleInference:
    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self._types = set(config.db_handle_types)
        self._names = set(config.db_handle_names)
        self._operations = set(config.db_operations)
        self._non_db_factories = set(config.non_db_factories)
        self._non_db_modules = set(config.non_db_modules)
        self._dependency_re = re.compile(config.db_dependency_pattern)
        self._factory_re = re.compile(config.db_factory_pattern)

    # --- classification helpers --------------------------------------------------------------

    def _annotation_handle(self, annotation: ast.AST | None) -> HandleInfo | None:
        if annotation is None:
            return None
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        for node in ast.walk(annotation):
            if isinstance(node, ast.Call) and terminal_name(node.func) == "Depends":
                dep = self._dependency_handle(node)
                if dep is not None:
                    return dep
            dotted = dotted_name(node) if isinstance(node, (ast.Name, ast.Attribute)) else None
            if dotted is None:
                continue
            root = dotted.split(".", 1)[0]
            if root in self._non_db_modules and "." in dotted:
                return HandleInfo(False, "annotation", dotted)
            if dotted.rsplit(".", 1)[-1] in self._types:
                return HandleInfo(True, "annotation", dotted)
        return None

    def _dependency_handle(self, call: ast.Call) -> HandleInfo | None:
        if terminal_name(call.func) != "Depends" or not call.args:
            return None
        provider = terminal_name(call.args[0])
        if provider and self._dependency_re.search(provider):
            return HandleInfo(True, "dependency", f"Depends({provider})")
        return None

    def _factory_handle(self, value: ast.AST | None) -> HandleInfo | None:
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            return None
        dotted = dotted_name(value.func)
        name = terminal_name(value.func)
        if dotted in self._non_db_factories or name in self._non_db_factories:
            return HandleInfo(False, "factory", f"{dotted or name}()")
        if dotted and dotted.split(".", 1)[0] in self._non_db_modules:
            return HandleInfo(False, "factory", f"{dotted}()")
        if name and not name.lower().endswith("maker") and self._factory_re.search(name):
            return HandleInfo(True, "factory", f"{dotted or name}()")
        return None

    # --- per-scope inference ----------------------------------------------------------------

    def module_handles(self, tree: ast.Module) -> dict[str, HandleInfo]:
        handles: dict[str, HandleInfo] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                info = self._factory_handle(stmt.value)
                if info is not None:
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            handles[target.id] = HandleInfo(info.is_db, "module_factory", info.detail)
        return handles

    def scope_handles(self, scope: FunctionScope, module_handles: dict[str, HandleInfo]) -> dict[str, HandleInfo]:
        """Names in ``scope`` with explicit DB/non-DB evidence (name heuristic not included)."""
        handles = dict(module_handles)
        node = scope.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            defaults: list[ast.AST | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
            pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
            for arg, default in pairs:
                info = self._annotation_handle(arg.annotation)
                if info is None and isinstance(default, ast.Call):
                    info = self._dependency_handle(default)
                if info is not None:
                    handles[arg.arg] = info

        for child in _walk_scope(node):
            if isinstance(child, ast.Assign):
                info = self._factory_handle(child.value)
                targets = [t for t in child.targets if isinstance(t, ast.Name)]
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                info = self._annotation_handle(child.annotation) or self._factory_handle(child.value)
                targets = [child.target]
            elif isinstance(child, ast.withitem) and isinstance(child.optional_vars, ast.Name):
                info = self._factory_handle(child.context_expr)
                targets = [child.optional_vars]
            else:
                continue
            if info is None:
                continue
            for target in targets:
                previous = handles.get(target.id)
                if previous is None or info.is_db or not previous.is_db:
                    handles[target.id] = info
        return handles

    def resolve_receiver(self, receiver: ast.AST, handles: dict[str, HandleInfo]) -> HandleInfo | None:
        """Is ``receiver`` (the object a method is called on) a database handle?"""
        if isinstance(receiver, ast.Name):
            info = handles.get(receiver.id)
            if info is not None:
                return info if info.is_db else None
            if receiver.id in self._names:
                return HandleInfo(True, "name_heuristic", receiver.id)
            return None
        if isinstance(receiver, ast.Attribute) and isinstance(receiver.value, (ast.Name, ast.Attribute)):
            # self.db, self._session, request.state.db
            if receiver.attr.lstrip("_") in self._names:
                return HandleInfo(True, "name_heuristic", dotted_name(receiver) or receiver.attr)
        return None

    def db_chain(self, node: ast.AST, handles: dict[str, HandleInfo]) -> tuple[DbOperation, list[str]] | None:
        """If ``node`` is a method chain starting at a DB operation (``db.query(X).filter().all()``),
        return the operation and the chain's method names, innermost first."""
        base, methods = unwind_chain(node)
        if not methods or methods[0] not in self._operations:
            return None
        info = self.resolve_receiver(base, handles)
        if info is None:
            return None
        return DbOperation(method=methods[0], receiver=ast.unparse(base), handle=info), methods

    def db_operation(self, call: ast.Call, handles: dict[str, HandleInfo]) -> DbOperation | None:
        """If ``call`` is ``<db handle>.<db method>(...)``, describe it."""
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in self._operations:
            return None
        info = self.resolve_receiver(func.value, handles)
        if info is None:
            return None
        return DbOperation(method=func.attr, receiver=ast.unparse(func.value), handle=info)


def unwind_chain(node: ast.AST) -> tuple[ast.AST, list[str]]:
    """``await a.b(x).c().d()`` -> (``a``, ["b", "c", "d"])."""
    methods: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Await):
            current = current.value
        elif isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            methods.append(current.func.attr)
            current = current.func.value
        else:
            return current, list(reversed(methods))


def _walk_scope(node: ast.AST):
    """ast.walk over a scope body, not descending into nested functions or classes."""
    stack = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


walk_scope = _walk_scope
