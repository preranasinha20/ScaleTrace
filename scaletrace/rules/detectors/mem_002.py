"""MEM-002: unbounded accumulation in a long-lived collection.

Narrowed on purpose (docs/decisions.md, D4): only collections that outlive a request are
considered, i.e. module-level collections and class-level attributes. Growing a *local* list
in a loop is how most response bodies are built and is not reported.

Reported: a function that grows such a collection (``.append``, ``.add``, ``.update``, ``x[k] = v``,
``x += ...``) when no bound is visible anywhere in the module (``.clear()``, ``.pop()``,
``del x[...]``, a ``len(x)`` comparison, ``deque(maxlen=...)``).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from scaletrace.analyzer.module import ModuleContext
from scaletrace.analyzer.project import MODULE_SCOPE, FunctionInfo, ProjectIndex
from scaletrace.analyzer.scopes import terminal_name
from scaletrace.rules.detectors.base import Detector, RawMatch
from scaletrace.schemas import StaticEvidence

PATTERN_GLOBAL = "mutation of module/global collection"
PATTERN_LOOP = "append inside loop"
COLLECTION_FACTORIES = {"list", "dict", "set", "defaultdict", "OrderedDict", "Counter", "deque"}


@dataclass(frozen=True)
class Collection:
    name: str
    owner: str  # "module" or the class name
    line: int
    kind: str


def _collection_kind(value: ast.AST | None) -> str | None:
    if isinstance(value, ast.List):
        return "list"
    if isinstance(value, ast.Dict):
        return "dict"
    if isinstance(value, ast.Set):
        return "set"
    if isinstance(value, ast.Call) and terminal_name(value.func) in COLLECTION_FACTORIES:
        if terminal_name(value.func) == "deque" and any(k.arg == "maxlen" for k in value.keywords):
            return None
        if terminal_name(value.func) == "deque" and len(value.args) >= 2:
            return None
        return terminal_name(value.func)
    return None


def _assigned_collections(body: list[ast.stmt], owner: str) -> list[Collection]:
    found = []
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        kind = _collection_kind(value)
        if kind is None:
            continue
        found.extend(Collection(t.id, owner, stmt.lineno, kind) for t in targets if isinstance(t, ast.Name))
    return found


class UnboundedAccumulationDetector(Detector):
    rule_id = "MEM-002"

    def _visible_bound(self, ctx: ModuleContext, collection: Collection) -> str | None:
        bounding = set(self.config.collection_bounding_methods)
        for node in ast.walk(ctx.tree):
            target = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in bounding:
                target = node.func.value
                reason = f".{node.func.attr}()"
            elif isinstance(node, ast.Delete):
                for item in node.targets:
                    if isinstance(item, ast.Subscript) and self._refers_to(item.value, collection):
                        return "del statement"
            elif isinstance(node, ast.Compare) and any(
                isinstance(side, ast.Call) and terminal_name(side.func) == "len" and side.args
                and self._refers_to(side.args[0], collection)
                for side in [node.left, *node.comparators]
            ):
                return "len() comparison"
            if target is not None and self._refers_to(target, collection):
                return reason
        return None

    @staticmethod
    def _refers_to(node: ast.AST, collection: Collection) -> bool:
        if collection.owner == "module":
            return isinstance(node, ast.Name) and node.id == collection.name
        return (
            isinstance(node, ast.Attribute)
            and node.attr == collection.name
            and isinstance(node.value, ast.Name)
            and node.value.id in ("self", "cls", collection.owner)
        )

    @staticmethod
    def _local_names(info: FunctionInfo) -> set[str]:
        node = info.scope.node
        names: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names.update(a.arg for a in args.posonlyargs + args.args + args.kwonlyargs)
            globals_declared = {n for s in ast.walk(node) if isinstance(s, ast.Global) for n in s.names}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store) and sub.id not in globals_declared:
                    names.add(sub.id)
        return names

    def _growth_sites(self, info: FunctionInfo, collection: Collection, project: ProjectIndex):
        growth = set(self.config.collection_growth_methods)
        for node, loops in project.walker.walk(info.scope.node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in growth:
                if self._refers_to(node.func.value, collection):
                    yield node, f".{node.func.attr}()", loops
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and self._refers_to(target.value, collection):
                        yield node, "item assignment", loops
            elif isinstance(node, ast.AugAssign) and self._refers_to(node.target, collection):
                yield node, "augmented assignment", loops

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        for module, ctx in project.modules.items():
            collections = _assigned_collections(ctx.tree.body, "module")
            class_bodies = [n for n in ast.walk(ctx.tree) if isinstance(n, ast.ClassDef)]
            for cls in class_bodies:
                shadowed = {
                    t.attr for n in ast.walk(cls) if isinstance(n, (ast.Assign, ast.AnnAssign))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
                }
                collections += [c for c in _assigned_collections(cls.body, cls.name) if c.name not in shadowed]

            for collection in collections:
                bound = self._visible_bound(ctx, collection)
                if bound is not None:
                    continue
                for info in project.iter_functions():
                    if info.module != module or info.qualname == MODULE_SCOPE:
                        continue
                    if collection.owner == "module" and collection.name in self._local_names(info):
                        continue
                    for node, how, loops in self._growth_sites(info, collection, project):
                        owner = "module-level" if collection.owner == "module" else f"class-level ({collection.owner})"
                        description = (
                            f"`{collection.name}` is a {owner} {collection.kind} (line {collection.line}) that lives "
                            f"for the whole process; `{info.qualname}` grows it via {how}"
                        )
                        if loops:
                            description += f" inside `{loops[-1].header}`"
                        description += ". No clear(), pop(), size check or maxlen is visible, so it may grow across requests."
                        yield RawMatch(
                            info=info,
                            node=node,
                            summary=f"`{collection.name}` ({owner} {collection.kind}) grows via {how}",
                            evidence=[
                                StaticEvidence(
                                    pattern=PATTERN_LOOP if loops else PATTERN_GLOBAL,
                                    description=description,
                                    snippet=info.ctx.snippet(node),
                                    details={
                                        "collection": collection.name,
                                        "owner": collection.owner,
                                        "collection_kind": collection.kind,
                                        "defined_at_line": collection.line,
                                        "growth": how,
                                        "in_loop": bool(loops),
                                    },
                                )
                            ],
                        )
                        break  # one finding per (collection, function)
