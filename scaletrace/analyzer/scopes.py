"""Function scopes and FastAPI route detection."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
APP_FACTORIES = {"FastAPI", "APIRouter"}
# Fallback receiver names for route decorators when the app/router assignment is not visible
# in the same module (e.g. ``router`` imported from another file).
DEFAULT_ROUTER_NAMES = {"app", "router", "api"}

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    # Router/app variable the route was registered on: a local name here, rewritten to "module:name"
    # by the project index (which also applies include_router prefixes). Not part of equality.
    router: str | None = field(default=None, compare=False)

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class FunctionScope:
    """A function, method, or the module body (qualname ``<module>``)."""

    qualname: str
    node: FunctionNode | ast.Module
    class_name: str | None = None
    routes: list[Route] = field(default_factory=list)

    @property
    def is_async(self) -> bool:
        return isinstance(self.node, ast.AsyncFunctionDef)

    @property
    def endpoint(self) -> str | None:
        return ", ".join(str(route) for route in self.routes) or None


def terminal_name(node: ast.AST) -> str | None:
    """``a.b.c`` -> ``c``; ``name`` -> ``name``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` -> ``"a.b.c"``; ``None`` for anything that is not a plain name chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Module-level ``name = FastAPI(...)`` / ``APIRouter(prefix=...)`` assignments."""
    prefixes: dict[str, str] = {}
    for stmt in tree.body:
        # Plain and annotated assignments: `router = APIRouter()`, `ROUTER: typing.Final = fastapi.APIRouter()`.
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if not isinstance(value, ast.Call) or terminal_name(value.func) not in APP_FACTORIES:
            continue
        prefix = ""
        for keyword in value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                prefix = keyword.value.value
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _string_arg(call: ast.Call, keyword_name: str) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for keyword in call.keywords:
        if keyword.arg == keyword_name and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _routes_for(func: FunctionNode, prefixes: dict[str, str]) -> list[Route]:
    routes: list[Route] = []
    for decorator in func.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        receiver = decorator.func.value
        if not isinstance(receiver, ast.Name):
            continue
        name = receiver.id
        if name not in prefixes and name.lower() not in DEFAULT_ROUTER_NAMES and not name.lower().endswith("router"):
            continue
        path = _string_arg(decorator, "path")
        if path is None:
            continue
        full_path = prefixes.get(name, "") + path
        attr = decorator.func.attr
        if attr in HTTP_METHODS:
            routes.append(Route(attr.upper(), full_path, name))
        elif attr == "api_route":
            methods = ["GET"]
            for keyword in decorator.keywords:
                if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    methods = [
                        elt.value.upper() for elt in keyword.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
            routes.extend(Route(method, full_path, name) for method in methods)
    return routes


def collect_scopes(tree: ast.Module) -> list[FunctionScope]:
    """All function/method scopes in the module, plus the module body itself."""
    prefixes = router_prefixes(tree)
    scopes = [FunctionScope(qualname="<module>", node=tree)]

    def visit(body: list[ast.stmt], prefix: str, class_name: str | None) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{stmt.name}"
                scopes.append(
                    FunctionScope(qualname=qualname, node=stmt, class_name=class_name, routes=_routes_for(stmt, prefixes))
                )
                visit(stmt.body, f"{qualname}.", None)
            elif isinstance(stmt, ast.ClassDef):
                visit(stmt.body, f"{prefix}{stmt.name}.", stmt.name)
            else:
                # Functions defined inside if/try/with blocks at the same level.
                for field_name in ("body", "orelse", "finalbody", "handlers"):
                    nested = getattr(stmt, field_name, None)
                    if isinstance(nested, list):
                        inner = [s.body if isinstance(s, ast.ExceptHandler) else [s] for s in nested]
                        visit([s for group in inner for s in group], prefix, class_name)

    visit(tree.body, "", None)
    return scopes
