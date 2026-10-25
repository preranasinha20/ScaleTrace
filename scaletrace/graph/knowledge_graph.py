"""Knowledge graph construction, runtime merging, risk paths and JSON (de)serialization.

Nodes
    endpoint  ``endpoint:GET /orders``
    function  ``function:app.main:list_orders`` (functions, methods, module bodies with activity)
    resource  ``resource:database``, ``resource:external:api.example.com``

Edges (directed, ``kind`` attribute; a MultiDiGraph keyed by kind)
    routes_to       endpoint -> handler function
    depends_on      function -> FastAPI dependency function (``Depends(f)``)
    calls           function -> function (resolved statically, and/or observed at runtime)
    queries         function -> database
    calls_external  function -> external service

Static edges are an approximation of the real call structure. Runtime merging marks edges
that were actually observed (``observed=True``) and adds observed edges the static analysis
missed (``static=False``), which is the "observed-edge augmentation" studied in the evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import networkx as nx

from scaletrace.analyzer.project import MODULE_SCOPE, FunctionInfo, ProjectIndex, unbounded
from scaletrace.schemas import Finding
from scaletrace.schemas.runtime import RuntimeReport

GRAPH_FORMAT_VERSION = "1.0"
DB_NODE = "resource:database"
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


class NodeKind(str, Enum):
    ENDPOINT = "endpoint"
    FUNCTION = "function"
    RESOURCE = "resource"


class EdgeKind(str, Enum):
    ROUTES_TO = "routes_to"
    DEPENDS_ON = "depends_on"
    CALLS = "calls"
    QUERIES = "queries"
    CALLS_EXTERNAL = "calls_external"


CONTROL_EDGES = {EdgeKind.ROUTES_TO.value, EdgeKind.DEPENDS_ON.value, EdgeKind.CALLS.value}


def endpoint_node(endpoint: str) -> str:
    return f"endpoint:{endpoint}"


def function_node(func_id: str) -> str:
    return f"function:{func_id}"


def external_node(service: str) -> str:
    return f"resource:external:{service}"


def _function_attrs(info: FunctionInfo) -> dict[str, Any]:
    facts = info.facts
    return {
        "kind": NodeKind.FUNCTION.value,
        "label": info.qualname if info.qualname != MODULE_SCOPE else f"{info.module} (module)",
        "func_id": info.func_id,
        "module": info.module,
        "file": info.file,
        "line": info.line,
        "end_line": info.end_line,
        "is_async": info.scope.is_async,
        "is_handler": bool(info.scope.routes),
        "loc": facts.loc,
        "loop_count": facts.loop_count,
        "max_loop_depth": facts.max_loop_depth,
        "n_calls": sum(1 for c in facts.calls if c.kind == "call"),
        "n_calls_in_loop": sum(1 for c in facts.calls if c.kind == "call" and unbounded(c.loops)),
        "n_db_ops": len(facts.db_accesses),
        "n_db_ops_in_loop": sum(1 for a in facts.db_accesses if unbounded(a.loops)),
        "n_external_calls": len(facts.external_accesses),
        "n_external_in_loop": sum(1 for a in facts.external_accesses if unbounded(a.loops)),
        "unresolved_calls": facts.unresolved_calls,
        "findings": [],
        "rules": [],
        "max_severity": None,
        "observed": False,
    }


def _is_interesting(info: FunctionInfo) -> bool:
    if info.qualname != MODULE_SCOPE:
        return True
    facts = info.facts
    return bool(facts.calls or facts.db_accesses or facts.external_accesses)


def _add_edge(graph: nx.MultiDiGraph, source: str, target: str, kind: EdgeKind, line: int | None, in_loop: bool) -> None:
    key = kind.value
    if graph.has_edge(source, target, key):
        data = graph.edges[source, target, key]
        data["count"] += 1
        data["in_loop"] = data["in_loop"] or in_loop
        if line is not None:
            data["lines"].append(line)
    else:
        graph.add_edge(
            source, target, key=key, kind=key, count=1, in_loop=in_loop,
            lines=[line] if line is not None else [], static=True, observed=False, observed_calls_per_request=None,
        )


def build_knowledge_graph(project: ProjectIndex, findings: list[Finding]) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(format_version=GRAPH_FORMAT_VERSION)

    for info in project.iter_functions():
        if _is_interesting(info):
            graph.add_node(function_node(info.func_id), **_function_attrs(info))

    for endpoint, handler in sorted(project.endpoints().items()):
        method, _, path = endpoint.partition(" ")
        graph.add_node(
            endpoint_node(endpoint), kind=NodeKind.ENDPOINT.value, label=endpoint, method=method, path=path,
            handler=handler.func_id, file=handler.file, line=handler.line, observed=False,
        )
        _add_edge(graph, endpoint_node(endpoint), function_node(handler.func_id), EdgeKind.ROUTES_TO, None, False)

    for info in project.iter_functions():
        source = function_node(info.func_id)
        if source not in graph:
            continue
        for call in info.facts.calls:
            if call.callee is None or function_node(call.callee) not in graph:
                continue
            kind = EdgeKind.DEPENDS_ON if call.kind == "dependency" else EdgeKind.CALLS
            _add_edge(graph, source, function_node(call.callee), kind, call.node.lineno, unbounded(call.loops))
        for access in info.facts.db_accesses:
            if DB_NODE not in graph:
                graph.add_node(DB_NODE, kind=NodeKind.RESOURCE.value, label="Database", resource_type="database", observed=False)
            _add_edge(graph, source, DB_NODE, EdgeKind.QUERIES, access.node.lineno, unbounded(access.loops))
        for access in info.facts.external_accesses:
            target = external_node(access.info.service)
            if target not in graph:
                graph.add_node(target, kind=NodeKind.RESOURCE.value, label=access.info.service, resource_type="external", observed=False)
            _add_edge(graph, source, target, EdgeKind.CALLS_EXTERNAL, access.node.lineno, unbounded(access.loops))

    attach_findings(graph, project, findings)
    return graph


def attach_findings(graph: nx.MultiDiGraph, project: ProjectIndex, findings: list[Finding]) -> None:
    for finding in findings:
        info = project.function_at(finding.file, finding.function)
        if info is None:
            continue
        node = function_node(info.func_id)
        if node not in graph:
            graph.add_node(node, **_function_attrs(info))
        data = graph.nodes[node]
        data["findings"].append(finding.finding_id)
        if finding.rule_id not in data["rules"]:
            data["rules"].append(finding.rule_id)
        severity = finding.severity.value
        if data["max_severity"] is None or SEVERITY_RANK[severity] > SEVERITY_RANK[data["max_severity"]]:
            data["max_severity"] = severity


def merge_runtime(graph: nx.MultiDiGraph, runtime: RuntimeReport) -> None:
    """Mark observed nodes/edges and add observed call edges missing from the static graph.

    Values are taken from the largest measured input size of each endpoint.
    """
    for endpoint_runtime in runtime.endpoints:
        measurements = endpoint_runtime.by_size()
        if not measurements:
            continue
        largest = measurements[-1]
        ep_node = endpoint_node(endpoint_runtime.endpoint)
        if ep_node in graph:
            graph.nodes[ep_node]["observed"] = True
        for func_id, calls in largest.function_calls_per_request.items():
            node = function_node(func_id)
            if node in graph:
                data = graph.nodes[node]
                data["observed"] = True
                data["observed_calls_per_request"] = max(calls, data.get("observed_calls_per_request") or 0.0)
        for func_id, queries in largest.queries_by_function_per_request.items():
            node = function_node(func_id)
            if node not in graph:
                continue
            data = graph.nodes[node]
            data["observed"] = True
            data["observed_queries_per_request"] = max(queries, data.get("observed_queries_per_request") or 0.0)
            if DB_NODE not in graph:
                graph.add_node(DB_NODE, kind=NodeKind.RESOURCE.value, label="Database", resource_type="database", observed=True)
            graph.nodes[DB_NODE]["observed"] = True
            _mark_observed(graph, node, DB_NODE, EdgeKind.QUERIES, queries)
        for edge in largest.call_edges:
            target = function_node(edge.callee)
            source = ep_node if edge.caller == "<request>" else function_node(edge.caller)
            if source not in graph or target not in graph:
                continue
            if graph.nodes[source]["kind"] == NodeKind.ENDPOINT.value:
                kind = EdgeKind.ROUTES_TO if graph.has_edge(source, target, EdgeKind.ROUTES_TO.value) else None
                if kind is None:
                    continue  # framework-invoked dependency; already linked via depends_on
            elif graph.has_edge(source, target, EdgeKind.DEPENDS_ON.value):
                kind = EdgeKind.DEPENDS_ON
            else:
                kind = EdgeKind.CALLS
            _mark_observed(graph, source, target, kind, edge.calls_per_request)


def _mark_observed(graph: nx.MultiDiGraph, source: str, target: str, kind: EdgeKind, calls: float) -> None:
    key = kind.value
    if not graph.has_edge(source, target, key):
        graph.add_edge(source, target, key=key, kind=key, count=0, in_loop=False, lines=[], static=False,
                       observed=True, observed_calls_per_request=calls)
        return
    data = graph.edges[source, target, key]
    data["observed"] = True
    data["observed_calls_per_request"] = max(calls, data.get("observed_calls_per_request") or 0.0)


@dataclass
class RiskPath:
    endpoint: str
    nodes: list[str]
    rules: list[str]
    max_severity: str
    findings: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

    def describe(self, graph: nx.MultiDiGraph) -> str:
        labels = [graph.nodes[n]["label"] for n in self.nodes]
        tail = f" -> {', '.join(graph.nodes[r]['label'] for r in self.resources)}" if self.resources else ""
        return f"{' -> '.join(labels)} [{', '.join(self.rules)}]{tail}"


def control_subgraph(graph: nx.MultiDiGraph) -> nx.DiGraph:
    """Simple DiGraph over control edges (routes_to / depends_on / calls) for path finding."""
    simple = nx.DiGraph()
    simple.add_nodes_from(graph.nodes)
    for source, target, data in graph.edges(data=True):
        if data["kind"] in CONTROL_EDGES:
            weight = 1.0 if data.get("observed") else 2.0
            if not simple.has_edge(source, target) or simple.edges[source, target]["weight"] > weight:
                simple.add_edge(source, target, weight=weight)
    return simple


def risk_paths(graph: nx.MultiDiGraph) -> list[RiskPath]:
    """For each endpoint, the shortest path to every reachable function that carries findings."""
    control = control_subgraph(graph)
    paths: list[RiskPath] = []
    for node, data in graph.nodes(data=True):
        if data["kind"] != NodeKind.ENDPOINT.value:
            continue
        lengths, routes = nx.single_source_dijkstra(control, node, weight="weight")
        for target in sorted(routes, key=lambda t: lengths[t]):
            target_data = graph.nodes[target]
            if target_data["kind"] != NodeKind.FUNCTION.value or not target_data.get("findings"):
                continue
            resources = sorted(
                v for _, v, d in graph.out_edges(target, data=True)
                if d["kind"] in (EdgeKind.QUERIES.value, EdgeKind.CALLS_EXTERNAL.value)
            )
            paths.append(
                RiskPath(
                    endpoint=data["label"],
                    nodes=routes[target],
                    rules=list(target_data["rules"]),
                    max_severity=target_data["max_severity"],
                    findings=list(target_data["findings"]),
                    resources=sorted(set(resources)),
                )
            )
    paths.sort(key=lambda p: (-SEVERITY_RANK.get(p.max_severity, 0), p.endpoint, len(p.nodes)))
    return paths


def graph_to_json(graph: nx.MultiDiGraph) -> dict[str, Any]:
    nodes = [{"id": node, **data} for node, data in graph.nodes(data=True)]
    edges = [{"source": s, "target": t, **data} for s, t, data in graph.edges(data=True)]
    return {"graph_format_version": GRAPH_FORMAT_VERSION, "nodes": nodes, "edges": edges}


def graph_from_json(data: dict[str, Any]) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(format_version=data.get("graph_format_version", GRAPH_FORMAT_VERSION))
    for node in data["nodes"]:
        attrs = dict(node)
        graph.add_node(attrs.pop("id"), **attrs)
    for edge in data["edges"]:
        attrs = dict(edge)
        source, target = attrs.pop("source"), attrs.pop("target")
        graph.add_edge(source, target, key=attrs["kind"], **attrs)
    return graph
