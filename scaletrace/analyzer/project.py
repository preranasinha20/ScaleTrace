"""Project-wide index: functions, imports, call resolution and per-function facts.

Call resolution is a deliberately simple approximation (documented in docs/limitations.md):

* ``f()``          -> nested function, module-level function, or imported function;
* ``mod.f()``      -> function in an imported project module;
* ``self.m()``     -> method of the enclosing class;
* ``Cls()``        -> ``Cls.__init__`` when defined in the project;
* ``Depends(f)``   -> FastAPI dependency edge to ``f``.

Anything else (dynamic dispatch, callbacks, objects of unknown type) is left unresolved and
counted, so reports can say how much of the call graph is missing.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator
from dataclasses import dataclass, field

from scaletrace.analyzer.db_handles import DbHandleInference, DbOperation, HandleInfo, walk_scope
from scaletrace.analyzer.external import ExternalCallInference, ExternalCallInfo
from scaletrace.analyzer.loops import LoopContext, LoopWalker
from scaletrace.analyzer.module import ModuleContext
from scaletrace.analyzer.scopes import HTTP_METHODS, FunctionScope, Route, router_prefixes, terminal_name
from scaletrace.rules.config import DetectorConfig

MODULE_SCOPE = "<module>"
MAX_INCLUDE_DEPTH = 6
_BUILTINS = set(dir(builtins))


def module_name_for(rel_path: str) -> str:
    """``app/main.py`` -> ``app.main``; ``app/__init__.py`` -> ``app``."""
    parts = rel_path[:-3].split("/") if rel_path.endswith(".py") else rel_path.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


def unbounded(loops: tuple[LoopContext, ...]) -> bool:
    return bool(loops) and not all(loop.bounded for loop in loops)


@dataclass
class CallSite:
    callee: str | None
    callee_text: str
    node: ast.Call
    loops: tuple[LoopContext, ...]
    kind: str = "call"  # "call" or "dependency"


@dataclass
class DbAccess:
    operation: DbOperation
    node: ast.Call
    loops: tuple[LoopContext, ...]


@dataclass
class ExternalAccess:
    info: ExternalCallInfo
    node: ast.Call
    loops: tuple[LoopContext, ...]


@dataclass
class FunctionFacts:
    calls: list[CallSite] = field(default_factory=list)
    db_accesses: list[DbAccess] = field(default_factory=list)
    external_accesses: list[ExternalAccess] = field(default_factory=list)
    unresolved_calls: int = 0
    loop_count: int = 0
    max_loop_depth: int = 0
    loc: int = 0


@dataclass
class FunctionInfo:
    func_id: str
    module: str
    scope: FunctionScope
    ctx: ModuleContext
    db_handles: dict[str, HandleInfo] = field(default_factory=dict)
    facts: FunctionFacts = field(default_factory=FunctionFacts)

    @property
    def qualname(self) -> str:
        return self.scope.qualname

    @property
    def file(self) -> str:
        return self.ctx.rel_path

    @property
    def line(self) -> int:
        return getattr(self.scope.node, "lineno", 1)

    @property
    def end_line(self) -> int | None:
        return getattr(self.scope.node, "end_lineno", None)

    @property
    def source(self) -> str:
        if self.qualname == MODULE_SCOPE:
            return ""
        return ast.get_source_segment(self.ctx.source, self.scope.node) or ""


class ProjectIndex:
    """All analyzable modules of one repository, with resolved calls between their functions."""

    def __init__(self, modules: list[ModuleContext], config: DetectorConfig) -> None:
        self.config = config
        self.db = DbHandleInference(config)
        self.http = ExternalCallInference(config)
        self.walker = LoopWalker(config.bounded_loop_max_iterations)

        self.modules: dict[str, ModuleContext] = {module_name_for(m.rel_path): m for m in modules}
        self.functions: dict[str, FunctionInfo] = {}
        self._by_location: dict[tuple[str, str], FunctionInfo] = {}
        self.classes: dict[str, str] = {}  # class id "mod:Cls" -> module
        self.symbols: dict[str, dict[str, tuple[str, str]]] = {}
        self.external_imports: dict[str, dict[str, str]] = {}
        self._callers: dict[str, set[str]] | None = None

        for module, ctx in self.modules.items():
            for scope in ctx.scopes:
                info = FunctionInfo(func_id=f"{module}:{scope.qualname}", module=module, scope=scope, ctx=ctx)
                self.functions[info.func_id] = info
                self._by_location[(ctx.rel_path, scope.qualname)] = info
            for node in ast.walk(ctx.tree):
                if isinstance(node, ast.ClassDef):
                    self.classes[f"{module}:{node.name}"] = module
        self._module_vars = {
            module: {t.id for stmt in ctx.tree.body if isinstance(stmt, (ast.Assign, ast.AnnAssign))
                     for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]) if isinstance(t, ast.Name)}
            for module, ctx in self.modules.items()
        }
        for module, ctx in self.modules.items():
            self.symbols[module], self.external_imports[module] = self._module_symbols(module, ctx)
        self._resolve_routes()
        self._module_db_handles = {m: self.db.module_handles(ctx.tree) for m, ctx in self.modules.items()}
        for info in self.functions.values():
            self._extract_facts(info)

    # --- lookups --------------------------------------------------------------------------------

    def function_at(self, rel_path: str, qualname: str) -> FunctionInfo | None:
        return self._by_location.get((rel_path, qualname))

    def iter_functions(self) -> Iterator[FunctionInfo]:
        return iter(self.functions.values())

    def endpoints(self) -> dict[str, FunctionInfo]:
        """``"GET /path"`` -> handler function."""
        found: dict[str, FunctionInfo] = {}
        for info in self.functions.values():
            for route in info.scope.routes:
                found.setdefault(str(route), info)
        return found

    # --- imports and symbols --------------------------------------------------------------------

    def _resolve_module(self, name: str) -> str | None:
        if name in self.modules:
            return name
        # src/ layouts: "pkg.x" may be indexed as "src.pkg.x".
        matches = [m for m in self.modules if m.endswith("." + name)]
        return matches[0] if len(matches) == 1 else None

    def _absolute_import(self, module: str, ctx: ModuleContext, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        package_parts = module.split(".")
        if not ctx.rel_path.endswith("__init__.py"):
            package_parts = package_parts[:-1]
        if node.level > 1:
            package_parts = package_parts[: len(package_parts) - (node.level - 1)]
        base = ".".join(package_parts)
        if node.module:
            return f"{base}.{node.module}" if base else node.module
        return base

    def _module_symbols(self, module: str, ctx: ModuleContext) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        symbols: dict[str, tuple[str, str]] = {}
        external: dict[str, str] = {}
        for stmt in ctx.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols[stmt.name] = ("function", f"{module}:{stmt.name}")
            elif isinstance(stmt, ast.ClassDef):
                symbols[stmt.name] = ("class", f"{module}:{stmt.name}")
        for name in self._module_vars.get(module, ()):
            symbols.setdefault(name, ("variable", f"{module}:{name}"))
        for node in ast.walk(ctx.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    target = alias.name if alias.asname else alias.name.split(".", 1)[0]
                    resolved = self._resolve_module(target)
                    if resolved:
                        symbols.setdefault(local, ("module", resolved))
                    else:
                        external.setdefault(local, target)
            elif isinstance(node, ast.ImportFrom):
                source = self._absolute_import(module, ctx, node)
                resolved_source = self._resolve_module(source)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if resolved_source is None:
                        external.setdefault(local, f"{source}.{alias.name}")
                        continue
                    as_module = self._resolve_module(f"{resolved_source}.{alias.name}")
                    if f"{resolved_source}:{alias.name}" in self.functions:
                        symbols.setdefault(local, ("function", f"{resolved_source}:{alias.name}"))
                    elif f"{resolved_source}:{alias.name}" in self.classes:
                        symbols.setdefault(local, ("class", f"{resolved_source}:{alias.name}"))
                    elif as_module:
                        symbols.setdefault(local, ("module", as_module))
                    elif alias.name in self._module_vars.get(resolved_source, ()):
                        symbols.setdefault(local, ("variable", f"{resolved_source}:{alias.name}"))
        return symbols, external

    # --- route registration -------------------------------------------------------------------

    def _variable(self, module: str, node: ast.AST) -> str | None:
        """``router`` / ``routes.router`` -> "module:name" of a module-level variable, if resolvable."""
        symbols = self.symbols.get(module, {})
        if isinstance(node, ast.Name):
            kind, target = symbols.get(node.id, ("", ""))
            return target if kind == "variable" else None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            kind, target = symbols.get(node.value.id, ("", ""))
            if kind == "module" and node.attr in self._module_vars.get(target, ()):
                return f"{target}:{node.attr}"
        return None

    def _resolve_routes(self) -> None:
        """Add ``add_api_route`` registrations and apply ``include_router(..., prefix=...)`` prefixes.

        Decorator routes are already in each scope (with the router's own ``APIRouter(prefix=)``).
        Here every route gets its router as "module:name"; routes registered with
        ``router.add_api_route(path, endpoint, methods=[...])`` are attached to the resolved endpoint
        function; and the prefixes of every ``include_router`` chain leading to the router are applied.
        """
        prefixes = {f"{m}:{name}": prefix for m, ctx in self.modules.items() for name, prefix in router_prefixes(ctx.tree).items()}
        registered: dict[str, list[Route]] = {}
        includes: dict[str, list[tuple[str, str]]] = {}
        for info in self.functions.values():
            if info.scope.routes:
                registered[info.func_id] = [
                    Route(r.method, r.path, self._variable(info.module, ast.Name(id=r.router)) if r.router else None)
                    for r in info.scope.routes
                ]
        for module, ctx in self.modules.items():
            module_scope = self.functions.get(f"{module}:{MODULE_SCOPE}")
            for node in ast.walk(ctx.tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                keywords = {k.arg: k.value for k in node.keywords if k.arg}
                if node.func.attr == "add_api_route" and module_scope is not None:
                    path_node = node.args[0] if node.args else keywords.get("path")
                    endpoint = node.args[1] if len(node.args) > 1 else keywords.get("endpoint")
                    if not (isinstance(path_node, ast.Constant) and isinstance(path_node.value, str)) or endpoint is None:
                        continue
                    target = self.resolve_callable(module_scope, endpoint)
                    if target is None:
                        continue
                    router = self._variable(module, node.func.value)
                    methods = ["GET"]
                    if isinstance(keywords.get("methods"), (ast.List, ast.Tuple, ast.Set)):
                        methods = [e.value.upper() for e in keywords["methods"].elts
                                   if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.lower() in HTTP_METHODS]
                    path = prefixes.get(router or "", "") + path_node.value
                    for method in methods:
                        route = Route(method, path, router)
                        if route not in registered.setdefault(target, []):
                            registered[target].append(route)
                elif node.func.attr == "include_router" and node.args:
                    child = self._variable(module, node.args[0])
                    parent = self._variable(module, node.func.value)
                    prefix = keywords.get("prefix")
                    text = prefix.value if isinstance(prefix, ast.Constant) and isinstance(prefix.value, str) else ""
                    if child is not None:
                        includes.setdefault(child, []).append((parent or "", text))

        def effective(router: str | None, depth: int = 0) -> list[str]:
            if router is None or router not in includes or depth > MAX_INCLUDE_DEPTH:
                return [""]
            # A parent router's own APIRouter(prefix=...) applies to everything included into it.
            return [outer + prefixes.get(parent, "") + prefix for parent, prefix in includes[router]
                    for outer in effective(parent or None, depth + 1)]

        for func_id, routes in registered.items():
            expanded: list[Route] = []
            for route in routes:
                for outer in effective(route.router):
                    candidate = Route(route.method, outer + route.path, route.router)
                    if candidate not in expanded:
                        expanded.append(candidate)
            self.functions[func_id].scope.routes = expanded

    # --- call resolution ------------------------------------------------------------------------

    def _class_member(self, class_id: str, member: str) -> str | None:
        candidate = f"{class_id}.{member}"
        return candidate if candidate in self.functions else None

    def resolve_callable(self, info: FunctionInfo, func: ast.AST) -> str | None:
        module = info.module
        symbols = self.symbols.get(module, {})
        if isinstance(func, ast.Name):
            # Nested function of this scope or an enclosing scope.
            parts = info.qualname.split(".") if info.qualname != MODULE_SCOPE else []
            for depth in range(len(parts), 0, -1):
                candidate = f"{module}:{'.'.join(parts[:depth])}.{func.id}"
                if candidate in self.functions:
                    return candidate
            kind, target = symbols.get(func.id, ("", ""))
            if kind == "function":
                return target
            if kind == "class":
                return self._class_member(target, "__init__")
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            receiver = func.value.id
            if receiver in ("self", "cls") and info.scope.class_name:
                class_qual = info.qualname.rsplit(".", 1)[0]
                return self._class_member(f"{module}:{class_qual}", func.attr)
            kind, target = symbols.get(receiver, ("", ""))
            if kind == "module":
                candidate = f"{target}:{func.attr}"
                if candidate in self.functions:
                    return candidate
                if candidate in self.classes:
                    return self._class_member(candidate, "__init__")
            if kind == "class":
                return self._class_member(target, func.attr)
        return None

    def _dependencies(self, info: FunctionInfo) -> Iterator[ast.Call]:
        node = info.scope.node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        args = node.args
        candidates: list[ast.AST] = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
        candidates += [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs if a.annotation is not None]
        for candidate in candidates:
            for sub in ast.walk(candidate):
                if isinstance(sub, ast.Call) and terminal_name(sub.func) == "Depends" and sub.args:
                    yield sub

    # --- facts ----------------------------------------------------------------------------------

    def _extract_facts(self, info: FunctionInfo) -> None:
        facts = info.facts
        scope = info.scope
        info.db_handles = self.db.scope_handles(scope, self._module_db_handles[info.module])
        http_handles = self.http.client_handles(scope, walk_scope)
        external_imports = self.external_imports.get(info.module, {})

        if info.qualname != MODULE_SCOPE and info.end_line:
            facts.loc = info.end_line - info.line + 1

        for dependency in self._dependencies(info):
            callee = self.resolve_callable(info, dependency.args[0])
            facts.calls.append(CallSite(callee, ast.unparse(dependency.args[0]), dependency, (), kind="dependency"))

        seen_loops: set[int] = set()
        for node, loops in self.walker.walk(scope.node):
            if loops:
                facts.max_loop_depth = max(facts.max_loop_depth, len(loops))
                for loop in loops:
                    seen_loops.add(hash((loop.line, loop.header)))
            if not isinstance(node, ast.Call):
                continue
            operation = self.db.db_operation(node, info.db_handles)
            if operation is not None:
                facts.db_accesses.append(DbAccess(operation, node, loops))
                continue
            external = self.http.external_call(node, http_handles, external_imports)
            if external is not None:
                facts.external_accesses.append(ExternalAccess(external, node, loops))
                continue
            if terminal_name(node.func) == "Depends":
                continue
            callee = self.resolve_callable(info, node.func)
            if callee is not None:
                facts.calls.append(CallSite(callee, ast.unparse(node.func), node, loops))
            elif not isinstance(node.func, ast.Name) or node.func.id not in _BUILTINS:
                facts.unresolved_calls += 1
        facts.loop_count = len(seen_loops)

    # --- reachability ---------------------------------------------------------------------------

    def callees(self, func_id: str) -> list[str]:
        info = self.functions.get(func_id)
        if info is None:
            return []
        return [c.callee for c in info.facts.calls if c.callee is not None]

    def resource_path(self, func_id: str, resource: str) -> list[str] | None:
        """Shortest call chain from ``func_id`` to a function that touches ``resource``
        ("database" or "external"), inclusive; None when unreachable."""
        frontier = [[func_id]]
        visited = {func_id}
        while frontier:
            next_frontier = []
            for path in frontier:
                info = self.functions.get(path[-1])
                if info is None:
                    continue
                touches = info.facts.db_accesses if resource == "database" else info.facts.external_accesses
                if touches:
                    return path
                for callee in self.callees(path[-1]):
                    if callee not in visited:
                        visited.add(callee)
                        next_frontier.append(path + [callee])
            frontier = next_frontier
        return None

    def endpoints_reaching(self, func_id: str) -> list[str]:
        """Endpoints whose handler (transitively) calls ``func_id``, sorted."""
        if self._callers is None:
            self._callers = {}
            for info in self.functions.values():
                for callee in self.callees(info.func_id):
                    self._callers.setdefault(callee, set()).add(info.func_id)
        callers = self._callers
        handlers: dict[str, list[str]] = {}
        for endpoint, info in self.endpoints().items():
            handlers.setdefault(info.func_id, []).append(endpoint)
        result: set[str] = set()
        stack, seen = [func_id], {func_id}
        while stack:
            current = stack.pop()
            result.update(handlers.get(current, []))
            for caller in callers.get(current, ()):
                if caller not in seen:
                    seen.add(caller)
                    stack.append(caller)
        return sorted(result)

