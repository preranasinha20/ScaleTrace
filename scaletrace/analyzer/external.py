"""Recognising calls to external HTTP services (requests, httpx, aiohttp)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from urllib.parse import urlparse

from scaletrace.analyzer.scopes import FunctionScope, dotted_name, terminal_name
from scaletrace.rules.config import DetectorConfig

UNKNOWN_SERVICE = "external service"


@dataclass(frozen=True)
class ExternalCallInfo:
    library: str
    method: str
    receiver: str
    service: str
    basis: str  # "module_function" | "client_annotation" | "client_factory" | "imported_function"


def service_from_url(node: ast.AST | None) -> str:
    """Host name from a literal URL argument, when visible in the source."""
    text: str | None = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        text = str(node.values[0].value)
    if text and "://" in text:
        host = urlparse(text).hostname
        if host:
            return host
    return UNKNOWN_SERVICE


class ExternalCallInference:
    def __init__(self, config: DetectorConfig) -> None:
        self._modules = set(config.http_modules)
        self._factories = set(config.http_client_factories)
        self._types = set(config.http_client_types)
        self._methods = set(config.http_methods)

    def _is_client_annotation(self, annotation: ast.AST | None) -> bool:
        if annotation is None:
            return False
        for node in ast.walk(annotation):
            dotted = dotted_name(node) if isinstance(node, (ast.Name, ast.Attribute)) else None
            if dotted and (dotted in self._types or dotted.rsplit(".", 1)[-1] in self._types and "." in dotted):
                return True
        return False

    def _is_client_factory(self, value: ast.AST | None) -> str | None:
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            return None
        dotted = dotted_name(value.func)
        if dotted in self._factories or terminal_name(value.func) in self._factories:
            root = (dotted or "").split(".", 1)[0]
            return root if root in self._modules else "http client"
        return None

    def client_handles(self, scope: FunctionScope, walk_scope) -> dict[str, str]:
        """Names bound to HTTP clients in ``scope``: name -> basis."""
        handles: dict[str, str] = {}
        node = scope.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs:
                if self._is_client_annotation(arg.annotation):
                    handles[arg.arg] = "client_annotation"
        for child in walk_scope(node):
            if isinstance(child, ast.Assign) and self._is_client_factory(child.value):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        handles[target.id] = "client_factory"
            elif isinstance(child, ast.withitem) and isinstance(child.optional_vars, ast.Name):
                if self._is_client_factory(child.context_expr):
                    handles[child.optional_vars.id] = "client_factory"
        return handles

    def external_call(
        self,
        call: ast.Call,
        handles: dict[str, str],
        imported: dict[str, str],
    ) -> ExternalCallInfo | None:
        """``imported`` maps local names to the external dotted name they were imported as."""
        func = call.func
        url_arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "url"), None)
        if isinstance(func, ast.Attribute) and func.attr in self._methods:
            receiver = func.value
            if isinstance(receiver, ast.Name):
                module = imported.get(receiver.id)
                if module in self._modules:
                    return ExternalCallInfo(module, func.attr, receiver.id, service_from_url(url_arg), "module_function")
                if receiver.id in handles:
                    return ExternalCallInfo("http client", func.attr, receiver.id, service_from_url(url_arg), handles[receiver.id])
        if isinstance(func, ast.Name) and func.id in imported:
            full = imported[func.id]
            root, _, method = full.rpartition(".")
            if root in self._modules and method in self._methods:
                return ExternalCallInfo(root, method, func.id, service_from_url(url_arg), "imported_function")
        return None
