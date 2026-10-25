"""Node features and graph tensors for the root-cause GNN.

A *sample* is one symptom on one application: the knowledge graph, the symptom's endpoint,
the candidate function nodes reachable from it, and (for training) which candidates are the
true root cause. Features are grouped so experiments can switch groups off (ablations):

=========  ===========================================================================
group      meaning
=========  ===========================================================================
type       node kind one-hot (endpoint / function / database / external)
structure  static code facts: size, loops, calls (in loops), DB/external accesses (in loops)
rules      rule-engine output: finding counts per core rule, max severity
symptom    conditioning on the symptom: is the endpoint, reachable, hop distance, symptom kind
runtime    measured at the symptom's endpoint: executed, call-count and attributed-query growth
code       code embedding of the function source (hashed tokens or a pretrained Transformer)
=========  ===========================================================================

The model never sees ground-truth labels or finding *statuses* (which are derived from runtime
evidence by the rule-based correlator); it gets the raw evidence and must combine it itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx

from scaletrace.analyzer.project import ProjectIndex
from scaletrace.diagnosis.correlate import match_runtime_endpoint
from scaletrace.graph.knowledge_graph import DB_NODE, NodeKind, control_subgraph, endpoint_node
from scaletrace.runtime.analysis import growth_exponent
from scaletrace.schemas.runtime import RuntimeReport

CORE_RULES = ["DB-001", "DB-003", "CMP-001", "MEM-001", "MEM-002", "EXT-001"]
SEVERITY_VALUE = {None: 0.0, "low": 0.33, "medium": 0.66, "high": 1.0}
STRUCTURE_KEYS = [
    "loc", "loop_count", "max_loop_depth", "n_calls", "n_calls_in_loop", "n_db_ops", "n_db_ops_in_loop",
    "n_external_calls", "n_external_in_loop", "unresolved_calls",
]
ALL_GROUPS = ["type", "structure", "rules", "symptom", "runtime", "code"]
VARIANT_GROUPS = {
    "static": ["type", "structure", "rules", "symptom", "code"],
    "combined": ["type", "structure", "rules", "symptom", "runtime", "code"],
    "combined_no_rules": ["type", "structure", "symptom", "runtime", "code"],
    "runtime_only": ["type", "symptom", "runtime"],
    "static_no_code": ["type", "structure", "rules", "symptom"],
    "combined_no_code": ["type", "structure", "rules", "symptom", "runtime"],
}


def feature_names(groups: list[str], code_dim: int) -> list[str]:
    names: list[str] = []
    for group in groups:
        if group == "type":
            names += ["type:endpoint", "type:function", "type:database", "type:external"]
        elif group == "structure":
            names += [f"structure:{k}" for k in STRUCTURE_KEYS] + ["structure:is_async", "structure:is_handler"]
        elif group == "rules":
            names += [f"rules:{r}" for r in CORE_RULES] + ["rules:max_severity"]
        elif group == "symptom":
            names += ["symptom:is_endpoint", "symptom:reachable", "symptom:proximity", "symptom:is_query_growth"]
        elif group == "runtime":
            names += [
                "runtime:available", "runtime:executed", "runtime:call_growth", "runtime:query_growth",
                "runtime:query_share", "runtime:log_calls", "runtime:log_queries", "runtime:endpoint_metric_growth",
            ]
        elif group == "code":
            names += [f"code:{i}" for i in range(code_dim)]
        else:
            raise ValueError(f"unknown feature group {group!r}")
    return names


@dataclass
class GraphTensors:
    """Plain-Python tensors; converted to torch by the model code."""

    nodes: list[str]
    x: list[list[float]]
    edge_index: list[tuple[int, int]]
    edge_weight: list[float]
    edge_kinds: list[str]
    candidates: list[int]
    labels: list[float] = field(default_factory=list)
    edge_attr: list[list[float]] = field(default_factory=list)  # one row per edge, EDGE_FEATURE_NAMES


EDGE_KINDS = ["routes_to", "depends_on", "calls", "queries", "calls_external"]
EDGE_FEATURE_NAMES = [f"edge:kind:{k}" for k in EDGE_KINDS] + [
    "edge:in_loop", "edge:observed", "edge:observed_call_growth", "edge:log_observed_calls",
]


def _edge_runtime(measurements) -> dict[tuple[str, str], tuple[float, float]]:
    """(caller, callee) -> (clipped call-count growth, log calls at the largest size) for one endpoint."""
    if not measurements:
        return {}
    sizes = [m.size for m in measurements]
    series: dict[tuple[str, str], list[float]] = {}
    for position, measurement in enumerate(measurements):
        for edge in measurement.call_edges:
            series.setdefault((edge.caller, edge.callee), [0.0] * len(measurements))[position] = edge.calls_per_request
    return {key: (_clip_growth(growth_exponent(sizes, values, offset=1.0)), math.log1p(values[-1])) for key, values in series.items()}


def _clip_growth(value: float | None) -> float:
    return 0.0 if value is None else max(0.0, min(2.0, value)) / 2.0


class FeatureBuilder:
    def __init__(self, groups: list[str], embedder, code_dim: int, source_transform=None) -> None:
        # source_transform(source, project) -> source; used by the identifier-renaming shortcut check.
        self.source_transform = source_transform
        self.groups = groups
        self.embedder = embedder
        self.code_dim = code_dim
        self.names = feature_names(groups, code_dim)
        self._embedding_cache: dict[str, list[float]] = {}

    def _embeddings(self, project: ProjectIndex | None, graph: nx.MultiDiGraph) -> dict[str, list[float]]:
        if "code" not in self.groups or project is None:
            return {}
        wanted: dict[str, str] = {}
        for node, data in graph.nodes(data=True):
            if data["kind"] == NodeKind.FUNCTION.value:
                info = project.functions.get(data["func_id"])
                source = info.source if info is not None else ""
                if self.source_transform is not None and source:
                    source = self.source_transform(source, project)
                wanted[node] = source
        missing = {s for s in wanted.values() if s not in self._embedding_cache}
        if missing:
            ordered = sorted(missing)
            for source, vector in zip(ordered, self.embedder.embed(ordered)):
                self._embedding_cache[source] = vector
        return {node: self._embedding_cache[source] for node, source in wanted.items()}

    def build(
        self,
        graph: nx.MultiDiGraph,
        symptom_endpoint: str,
        symptom_kind: str,
        project: ProjectIndex | None,
        runtime: RuntimeReport | None,
        root_causes: set[str] | None = None,
    ) -> GraphTensors | None:
        start = endpoint_node(symptom_endpoint)
        if start not in graph:
            return None
        control = control_subgraph(graph)
        distances = nx.single_source_shortest_path_length(control, start)
        nodes = sorted(graph.nodes)
        index = {n: i for i, n in enumerate(nodes)}
        embeddings = self._embeddings(project, graph)

        endpoint_rt = match_runtime_endpoint(symptom_endpoint, runtime) if runtime is not None and "runtime" in self.groups else None
        measurements = endpoint_rt.by_size() if endpoint_rt is not None and len(endpoint_rt.measurements) >= 2 else []
        sizes = [m.size for m in measurements]
        endpoint_growth = None
        if measurements:
            series = [m.queries_per_request for m in measurements] if symptom_kind == "query_growth" else [m.latency_ms_p95 for m in measurements]
            endpoint_growth = growth_exponent(sizes, series, offset=1.0 if symptom_kind == "query_growth" else 0.0)

        x: list[list[float]] = []
        for node in nodes:
            data = graph.nodes[node]
            kind = data["kind"]
            is_function = kind == NodeKind.FUNCTION.value
            row: list[float] = []
            for group in self.groups:
                if group == "type":
                    row += [
                        float(kind == NodeKind.ENDPOINT.value), float(is_function),
                        float(node == DB_NODE), float(kind == NodeKind.RESOURCE.value and node != DB_NODE),
                    ]
                elif group == "structure":
                    if is_function:
                        row += [math.log1p(float(data.get(k) or 0)) for k in STRUCTURE_KEYS]
                        row += [float(bool(data.get("is_async"))), float(bool(data.get("is_handler")))]
                    else:
                        row += [0.0] * (len(STRUCTURE_KEYS) + 2)
                elif group == "rules":
                    rules = data.get("rules") or []
                    findings = data.get("findings") or []
                    row += [float(sum(1 for f in findings if f.startswith(r + ":"))) if r in rules else 0.0 for r in CORE_RULES]
                    row.append(SEVERITY_VALUE.get(data.get("max_severity"), 0.0))
                elif group == "symptom":
                    distance = distances.get(node)
                    row += [
                        float(node == start), float(distance is not None),
                        1.0 / (1.0 + distance) if distance is not None else 0.0,
                        float(symptom_kind == "query_growth"),
                    ]
                elif group == "runtime":
                    row += self._runtime_row(data, measurements, sizes, endpoint_growth, node == start)
                elif group == "code":
                    row += embeddings.get(node, [0.0] * self.code_dim)
            x.append(row)

        edge_index, edge_weight, edge_kinds, edge_attr = [], [], [], []
        use_observed = "runtime" in self.groups and runtime is not None
        per_edge = _edge_runtime(measurements) if use_observed else {}
        for source, target, data in graph.edges(data=True):
            edge_index.append((index[source], index[target]))
            weight = 1.0 + float(bool(data.get("in_loop")))
            observed = bool(use_observed and data.get("observed"))
            if observed:
                weight += 1.0
            edge_weight.append(weight)
            edge_kinds.append(data["kind"])
            caller = "<request>" if graph.nodes[source]["kind"] == NodeKind.ENDPOINT.value else graph.nodes[source].get("func_id")
            growth, log_calls = per_edge.get((caller, graph.nodes[target].get("func_id")), (0.0, 0.0))
            edge_attr.append([float(data["kind"] == k) for k in EDGE_KINDS]
                             + [float(bool(data.get("in_loop"))), float(observed), growth, log_calls])

        candidates = [index[n] for n in nodes if n in distances and graph.nodes[n]["kind"] == NodeKind.FUNCTION.value]
        labels = []
        if root_causes is not None:
            labels = [1.0 if graph.nodes[n].get("func_id") in root_causes else 0.0 for n in nodes]
        return GraphTensors(nodes, x, edge_index, edge_weight, edge_kinds, candidates, labels, edge_attr)

    @staticmethod
    def _runtime_row(data, measurements, sizes, endpoint_growth, is_start) -> list[float]:
        if not measurements:
            return [0.0] * 8
        func_id = data.get("func_id")
        if func_id is None:
            metric = _clip_growth(endpoint_growth) if is_start else 0.0
            return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, metric]
        calls = [m.function_calls_per_request.get(func_id, 0.0) for m in measurements]
        queries = [m.queries_by_function_per_request.get(func_id, 0.0) for m in measurements]
        total_queries = measurements[-1].queries_per_request or 0.0
        executed = float(any(c > 0 for c in calls) or any(q > 0 for q in queries))
        return [
            1.0,
            executed,
            _clip_growth(growth_exponent(sizes, calls, offset=1.0)),
            _clip_growth(growth_exponent(sizes, queries, offset=1.0)),
            queries[-1] / total_queries if total_queries > 0 else 0.0,
            math.log1p(calls[-1]),
            math.log1p(queries[-1]),
            0.0,
        ]
