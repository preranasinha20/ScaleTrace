"""Backtracking diagnosis: from a symptom to ranked candidate code and an explained path.

Scoring (a proposed heuristic, not a validated formula -- see docs/limitations.md):

    Risk(v) = (w_s*S(v) + w_d*D(v) + w_p*P(v)) / (sum of weights of available terms)

S(v)  static evidence: severity-weighted findings at v, weighted by how relevant each rule is to
      the symptom (1.0 same metric, 0.5 other), normalized by the best candidate.
D(v)  runtime anomaly attributable to v: growth of the queries attributed to v (query symptoms)
      or of v's call count, scaled by v's share of the endpoint's queries; None when the runtime
      has no attribution data (then the term is dropped and the diagnosis is "approximate").
P(v)  importance: share of measured endpoints that executed v (runtime) or that can reach v
      (static fallback).

The top-ranked candidate is a *probable* cause. Nothing here proves causality.
"""

from __future__ import annotations

from collections.abc import Callable

import networkx as nx

from scaletrace.diagnosis.config import DiagnosisConfig
from scaletrace.diagnosis.correlate import RULE_METRICS, match_runtime_endpoint
from scaletrace.graph.knowledge_graph import DB_NODE, EdgeKind, NodeKind, control_subgraph, endpoint_node
from scaletrace.runtime.analysis import growth_exponent
from scaletrace.schemas import Finding, FindingStatus
from scaletrace.schemas.diagnosis import CandidateScore, Diagnosis, DiagnosticStep, Symptom
from scaletrace.schemas.runtime import RuntimeReport

SYMPTOM_METRIC = {"query_growth": "queries", "latency_growth": "compute_latency", "memory_growth": "memory"}

# A learned ranker returns {node_id: score in [0, 1]} for candidate nodes of a symptom.
Ranker = Callable[[nx.MultiDiGraph, Symptom, list[str]], dict[str, float]]


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


class Backtracker:
    def __init__(
        self,
        graph: nx.MultiDiGraph,
        findings: list[Finding],
        runtime: RuntimeReport | None,
        config: DiagnosisConfig,
        ranker: Ranker | None = None,
        ranker_name: str = "gnn",
    ) -> None:
        self.graph = graph
        self.findings = {f.finding_id: f for f in findings}
        self.runtime = runtime
        self.config = config
        self.ranker = ranker
        self.ranker_name = ranker_name
        self.control = control_subgraph(graph)
        self.endpoints = [n for n, d in graph.nodes(data=True) if d["kind"] == NodeKind.ENDPOINT.value]
        self.tracing = bool(runtime and runtime.instrumentation.get("call_tracing"))

    # --- candidate terms ------------------------------------------------------------------------

    def _func_id(self, node: str) -> str | None:
        return self.graph.nodes[node].get("func_id")

    def _static_raw(self, node: str, symptom: Symptom) -> float:
        wanted = SYMPTOM_METRIC.get(symptom.kind)
        total = 0.0
        for finding_id in self.graph.nodes[node].get("findings", []):
            finding = self.findings.get(finding_id)
            if finding is None or finding.status == FindingStatus.NOT_CONFIRMED:
                continue
            relevance = 1.0 if wanted is None or RULE_METRICS.get(finding.rule_id) == wanted else 0.5
            total += self.config.severity_weights[finding.severity.value] * relevance
        return total

    def _dynamic(self, node: str, symptom: Symptom) -> float | None:
        if self.runtime is None or symptom.endpoint is None:
            return None
        endpoint = match_runtime_endpoint(symptom.endpoint, self.runtime)
        if endpoint is None or len(endpoint.measurements) < 2:
            return None
        func_id = self._func_id(node)
        measurements = endpoint.by_size()
        sizes = [m.size for m in measurements]
        scores: list[float] = []
        if self.runtime.instrumentation.get("sqlalchemy_queries"):
            own = [m.queries_by_function_per_request.get(func_id, 0.0) for m in measurements]
            total = measurements[-1].queries_per_request or 0.0
            exponent = growth_exponent(sizes, own, offset=1.0)
            if exponent is not None and total > 0:
                share = own[-1] / total
                weight = 1.0 if symptom.kind == "query_growth" else 0.5
                scores.append(_clip(max(0.0, exponent) / self.config.dynamic_saturation) * share * weight)
        if self.tracing:
            calls = [m.function_calls_per_request.get(func_id, 0.0) for m in measurements]
            exponent = growth_exponent(sizes, calls, offset=1.0)
            if exponent is not None:
                scores.append(_clip(max(0.0, exponent) / self.config.dynamic_saturation))
        if self.tracing and self.config.edge_growth:
            outgoing: dict[str, list[float]] = {}
            for position, measurement in enumerate(measurements):
                for edge in measurement.call_edges:
                    if edge.caller == func_id:
                        outgoing.setdefault(edge.callee, [0.0] * len(measurements))[position] = edge.calls_per_request
            for values in outgoing.values():
                exponent = growth_exponent(sizes, values, offset=1.0)
                if exponent is not None:
                    scores.append(_clip(max(0.0, exponent) / self.config.dynamic_saturation))
        return max(scores) if scores else None

    def _importance(self, node: str) -> float:
        if self.runtime is not None and self.tracing:
            measured = [e for e in self.runtime.endpoints if e.measurements]
            if measured:
                func_id = self._func_id(node)
                hits = sum(1 for e in measured if any(func_id in m.function_calls_per_request for m in e.measurements))
                return hits / len(measured)
        if not self.endpoints:
            return 0.0
        reaching = sum(1 for e in self.endpoints if nx.has_path(self.control, e, node))
        return reaching / len(self.endpoints)

    # --- diagnosis ------------------------------------------------------------------------------

    def candidates_for(self, symptom: Symptom) -> tuple[str, list[str]]:
        if symptom.endpoint is not None and endpoint_node(symptom.endpoint) in self.graph:
            start = endpoint_node(symptom.endpoint)
            reachable = nx.descendants(self.control, start)
        elif symptom.resource == "database" and DB_NODE in self.graph:
            start = DB_NODE
            reachable = {u for u, _, d in self.graph.in_edges(DB_NODE, data=True) if d["kind"] == EdgeKind.QUERIES.value}
            reachable |= {a for n in list(reachable) for a in nx.ancestors(self.control, n)}
        else:
            return "", []
        nodes = sorted(n for n in reachable if self.graph.nodes[n]["kind"] == NodeKind.FUNCTION.value)
        return start, nodes

    def score(self, symptom: Symptom, nodes: list[str]) -> list[CandidateScore]:
        raw_static = {n: self._static_raw(n, symptom) for n in nodes}
        best_static = max(raw_static.values(), default=0.0)
        learned = self.ranker(self.graph, symptom, nodes) if self.ranker and nodes else {}
        scored = []
        for node in nodes:
            data = self.graph.nodes[node]
            static = raw_static[node] / best_static if best_static > 0 else 0.0
            dynamic = self._dynamic(node, symptom)
            importance = self._importance(node)
            terms = {"S": (self.config.w_static, static), "D": (self.config.w_dynamic, dynamic), "P": (self.config.w_importance, importance)}
            available = {k: v for k, v in terms.items() if v[1] is not None}
            weight_sum = sum(w for w, _ in available.values())
            risk = sum(w * v for w, v in available.values()) / weight_sum if weight_sum else 0.0
            scored.append(CandidateScore(
                node=node, label=data["label"], file=data.get("file"), line=data.get("line"),
                static_score=round(static, 4), dynamic_score=None if dynamic is None else round(dynamic, 4),
                importance=round(importance, 4), risk=round(risk, 4),
                missing_terms=[k for k in terms if k not in available],
                findings=list(data.get("findings", [])), rules=list(data.get("rules", [])),
                gnn_score=round(learned[node], 4) if node in learned else None,
            ))
        key = (lambda c: (-(c.gnn_score or 0.0), -c.risk)) if learned else (lambda c: (-c.risk, -(c.static_score or 0.0)))
        scored.sort(key=key)
        return scored

    def diagnose(self, symptom: Symptom, supporting: list[Symptom] | None = None) -> Diagnosis | None:
        start, nodes = self.candidates_for(symptom)
        if not start:
            return None
        candidates = self.score(symptom, nodes)
        top = candidates[0] if candidates else None
        path = self._path(start, top.node, symptom) if top else [start]
        at_top = [self.findings[f] for f in (top.findings if top else []) if f in self.findings]
        supported = [f for f in at_top if f.status == FindingStatus.CORRELATED_FINDING]
        top_findings = supported or [f for f in at_top if f.status != FindingStatus.NOT_CONFIRMED]
        unconfirmed = [f for f in at_top if f not in top_findings]
        explained = bool(top_findings) and (top.static_score or 0) > 0
        status = "correlated" if explained and supported else ("static_only" if explained else "unexplained")
        approximate = (
            self.runtime is None
            or (top is not None and top.dynamic_score is None)
            or not any(self.graph.edges[u, v, k].get("observed") for u, v, k in self._path_edges(path))
        )
        return Diagnosis(
            diagnosis_id=f"diagnosis:{symptom.symptom_id}",
            symptom=symptom,
            supporting_symptoms=supporting or [],
            start_node=start,
            status=status,
            approximate=approximate,
            ranking_method=self.ranker_name if self.ranker else "heuristic",
            path=path,
            path_labels=[self.graph.nodes[n]["label"] for n in path],
            candidates=candidates[: self.config.max_candidates],
            probable_cause=self._probable_cause(top, top_findings, status),
            steps=self._steps(symptom, supporting or [], path, top, top_findings, unconfirmed, status),
            related_findings=[f.finding_id for f in top_findings],
            verification_steps=self._verification(top_findings),
        )

    # --- explanation ----------------------------------------------------------------------------

    def _path(self, start: str, target: str, symptom: Symptom) -> list[str]:
        if start == DB_NODE:
            sources = [e for e in self.endpoints if nx.has_path(self.control, e, target)]
            path = nx.shortest_path(self.control, sources[0], target, weight="weight") if sources else [target]
            return path + [DB_NODE]
        path = nx.shortest_path(self.control, start, target, weight="weight")
        if symptom.resource == "database" and self.graph.has_edge(target, DB_NODE, EdgeKind.QUERIES.value):
            path.append(DB_NODE)
        return path

    def _path_edges(self, path: list[str]):
        for source, target in zip(path, path[1:]):
            for key in self.graph[source].get(target, {}):
                yield source, target, key

    def _edge_text(self, source: str, target: str) -> str:
        s, t = self.graph.nodes[source], self.graph.nodes[target]
        keys = self.graph[source][target]
        key = next(iter(keys))
        data = keys[key]
        verb = {
            EdgeKind.ROUTES_TO.value: "is handled by",
            EdgeKind.CALLS.value: "calls",
            EdgeKind.DEPENDS_ON.value: "depends on",
            EdgeKind.QUERIES.value: "queries",
            EdgeKind.CALLS_EXTERNAL.value: "calls",
        }[data["kind"]]
        target_label = t["label"] + ("()" if t["kind"] == NodeKind.FUNCTION.value else "")
        source_label = s["label"] + ("()" if s["kind"] == NodeKind.FUNCTION.value else "")
        text = f"{source_label} {verb} {target_label}"
        if data.get("in_loop"):
            text += " inside a loop"
        if data.get("observed"):
            calls = data.get("observed_calls_per_request")
            text += f" (observed at runtime{f', {calls:.0f}x per request at the largest size' if calls else ''})"
        else:
            text += " (static call graph)"
        return text + "."

    def _steps(self, symptom, supporting, path, top, findings, unconfirmed, status) -> list[DiagnosticStep]:
        steps: list[DiagnosticStep] = []

        def add(text: str, evidence: str) -> None:
            steps.append(DiagnosticStep(order=len(steps) + 1, text=text, evidence=evidence))

        add(f"Symptom: {symptom.description}", "runtime" if symptom.kind != "user_reported" else "inference")
        for extra in supporting:
            add(f"Also observed: {extra.description}", "runtime")
        for source, target in zip(path, path[1:]):
            add(self._edge_text(source, target), "graph")
        for finding in findings:
            evidence = finding.static_evidence[0] if finding.static_evidence else None
            add(
                f"{finding.rule_id} ({finding.rule_name}) at {finding.file}:{finding.line} in {finding.function}(): "
                + (f"`{evidence.snippet}` - {evidence.description}" if evidence else finding.message),
                "static",
            )
            for runtime_evidence in finding.runtime_evidence:
                if runtime_evidence.metric not in ("coverage",):
                    add(f"Runtime evidence for {finding.rule_id}: {runtime_evidence.description}", "runtime")
        if unconfirmed:
            listed = "; ".join(
                f"{f.rule_id} at {f.file}:{f.line} ({f.runtime_evidence[-1].description if f.runtime_evidence else 'no runtime support'})"
                for f in unconfirmed
            )
            add(f"Also present in this function but not confirmed by the workload: {listed}", "runtime")
        if top is not None and top.gnn_score is not None:
            add(f"The learned ranker scored {top.label}() at {top.gnn_score:.2f} (heuristic Risk {top.risk:.2f}).", "learned")
        add(self._probable_cause(top, findings, status), "inference")
        return steps

    @staticmethod
    def _probable_cause(top: CandidateScore | None, findings: list[Finding], status: str) -> str:
        if status == "unexplained" or top is None:
            return (
                "Cause unconfirmed: no static finding on the code reachable from this symptom explains it. "
                "The anomaly is reported without a source mapping."
            )
        rules = ", ".join(sorted({f"{f.rule_id} ({f.rule_name})" for f in findings}))
        location = f"{findings[0].file}:{findings[0].line}" if findings else f"{top.file}:{top.line}"
        if status == "correlated":
            return (
                f"Probable cause: {rules} in {top.label}() at {location}. Static and runtime evidence agree "
                f"under the tested workload; this is a probable cause, not a proof of causality."
            )
        return (
            f"Probable cause (static evidence only): {rules} in {top.label}() at {location}. "
            f"Runtime evidence does not confirm it yet."
        )

    @staticmethod
    def _verification(findings: list[Finding]) -> list[str]:
        steps: list[str] = []
        for finding in findings:
            for step in finding.verification_steps:
                if step not in steps:
                    steps.append(step)
        if findings:
            steps.append("apply the suggested correction and re-run the same workload to check that the growth disappears")
        return steps
