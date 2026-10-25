"""Comparing root-cause ranking methods on labelled applications.

Methods (all rank the same candidate set: functions reachable from the symptom endpoint in the
graph each method is allowed to see):

* ``random``              expected value of a uniformly random ranking (reference line)
* ``proximity``           closest to the symptom endpoint first (exposes "the handler is always the cause" shortcuts)
* ``rules_only``          severity-weighted count of rule findings at the node (static)
* ``heuristic_static``    Risk(v) with static evidence only (S, P)
* ``heuristic_combined``  Risk(v) with static + runtime evidence (S, D, P), correlated statuses
* ``gnn_<variant>``       each trained model (static / combined / combined_no_rules / ...)

Metrics: Top-1 and Top-3 accuracy and mean reciprocal rank (MRR) of the best-ranked true root
cause; ties count as the expected rank under random tie-breaking; a root cause missing from the
candidate set counts as a miss. Results are also broken down by defect kind.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import networkx as nx
import torch

from scaletrace.diagnosis import Backtracker, DiagnosisConfig
from scaletrace.graph.knowledge_graph import endpoint_node
from scaletrace.ml.dataset import AppBundle
from scaletrace.ml.features import FeatureBuilder
from scaletrace.ml.model import to_torch_full
from scaletrace.ml.train import expected_rank
from scaletrace.schemas.diagnosis import Symptom


@dataclass
class RankRecord:
    method: str
    app: str
    endpoint: str
    defect: str
    rank: float | None
    n_candidates: int
    seed: int | None = None

    @property
    def reciprocal(self) -> float:
        return 1.0 / self.rank if self.rank else 0.0


def summarize(records: list[RankRecord]) -> dict[str, dict]:
    def stats(rs: list[RankRecord]) -> dict:
        n = len(rs)
        if not n:
            return {"n": 0}
        return {
            "n": n,
            "top1": sum(1 for r in rs if r.rank is not None and r.rank <= 1.0) / n,
            "top3": sum(1 for r in rs if r.rank is not None and r.rank <= 3.0) / n,
            "mrr": sum(1.0 / r.rank for r in rs if r.rank) / n,
        }

    by_method: dict[str, list[RankRecord]] = defaultdict(list)
    for record in records:
        by_method[record.method].append(record)
    summary = {}
    for method, rs in by_method.items():
        by_defect: dict[str, list[RankRecord]] = defaultdict(list)
        for r in rs:
            by_defect[r.defect].append(r)
        summary[method] = {"overall": stats(rs), "by_defect": {d: stats(v) for d, v in sorted(by_defect.items())}}
    return summary


def _random_rank(n: int) -> float | None:
    return (n + 1) / 2 if n else None


def _symptom(label: dict) -> Symptom:
    return Symptom(
        symptom_id=f"eval:{label['endpoint']}", kind=label["kind"], endpoint=label["endpoint"],
        resource="database" if label["kind"] == "query_growth" else None, description="evaluation symptom",
    )


def heuristic_records(bundle: AppBundle, config: DiagnosisConfig) -> list[RankRecord]:
    records = []
    variants = [("rules_only", bundle.static_graph, bundle.static.report.findings, None)]
    variants.append(("heuristic_static", bundle.static_graph, bundle.static.report.findings, None))
    if bundle.runtime is not None:
        variants.append(("heuristic_combined", bundle.combined_graph, bundle.correlated_findings, bundle.runtime))
        variants.append(("heuristic_combined_edges", bundle.combined_graph, bundle.correlated_findings, bundle.runtime))
    for method, graph, findings, runtime in variants:
        method_config = config.model_copy(update={"edge_growth": True}) if method == "heuristic_combined_edges" else config
        tracker = Backtracker(graph, findings, runtime, method_config)
        for label in bundle.labels.get("symptoms", []):
            symptom = _symptom(label)
            start = endpoint_node(label["endpoint"])
            if start not in graph:
                records.append(RankRecord(method, bundle.name, label["endpoint"], label.get("defect", "?"), None, 0))
                continue
            _, nodes = tracker.candidates_for(symptom)
            roots = set(label["root_cause_functions"])
            positives = [graph.nodes[n].get("func_id") in roots for n in nodes]
            if not nodes or not any(positives):
                records.append(RankRecord(method, bundle.name, label["endpoint"], label.get("defect", "?"), None, len(nodes)))
                if method == "rules_only":
                    for baseline in ("random", "proximity"):
                        records.append(RankRecord(baseline, bundle.name, label["endpoint"], label.get("defect", "?"), None, len(nodes)))
                continue
            if method == "rules_only":
                scores = [tracker._static_raw(n, symptom) for n in nodes]
                distances = nx.single_source_shortest_path_length(tracker.control, start)
                closeness = [1.0 / (1.0 + distances.get(n, 99)) for n in nodes]
                records.append(RankRecord("proximity", bundle.name, label["endpoint"], label.get("defect", "?"), expected_rank(closeness, positives), len(nodes)))
            else:
                scored = {c.node: c.risk for c in tracker.score(symptom, nodes)}
                scores = [scored[n] for n in nodes]
            records.append(RankRecord(method, bundle.name, label["endpoint"], label.get("defect", "?"), expected_rank(scores, positives), len(nodes)))
            if method == "rules_only":
                records.append(RankRecord("random", bundle.name, label["endpoint"], label.get("defect", "?"), _random_rank(len(nodes)), len(nodes)))
    return records


def gnn_records(
    name: str,
    model: torch.nn.Module,
    checkpoint: dict,
    builder: FeatureBuilder,
    bundle: AppBundle,
    seed: int | None = None,
) -> list[RankRecord]:
    use_runtime = "runtime" in checkpoint["groups"]
    graph = bundle.combined_graph if use_runtime and bundle.combined_graph is not None else bundle.static_graph
    runtime = bundle.runtime if use_runtime else None
    records = []
    for label in bundle.labels.get("symptoms", []):
        tensors = builder.build(graph, label["endpoint"], label["kind"], bundle.static.project, runtime, set(label["root_cause_functions"]))
        defect = label.get("defect", "?")
        if tensors is None or not tensors.candidates or not any(tensors.labels[i] for i in tensors.candidates):
            records.append(RankRecord(name, bundle.name, label["endpoint"], defect, None, len(tensors.candidates) if tensors else 0, seed))
            continue
        x, edge_index, edge_weight, edge_attr, candidates, labels = to_torch_full(tensors, checkpoint["mean"], checkpoint["std"])
        with torch.no_grad():
            scores = model(x, edge_index, edge_weight, edge_attr)[candidates].tolist()
        positives = (labels[candidates] > 0).tolist()
        records.append(RankRecord(name, bundle.name, label["endpoint"], defect, expected_rank(scores, positives), len(scores), seed))
    return records


def render_markdown(summary: dict[str, dict], title: str) -> str:
    order = ["random", "proximity", "rules_only", "heuristic_static", "heuristic_combined"]
    methods = [m for m in order if m in summary] + sorted(m for m in summary if m not in order)
    lines = [f"## {title}", "", "| Method | n | Top-1 | Top-3 | MRR |", "|---|---|---|---|---|"]
    for method in methods:
        s = summary[method]["overall"]
        if s.get("n"):
            lines.append(f"| {method} | {s['n']} | {s['top1']:.2f} | {s['top3']:.2f} | {s['mrr']:.2f} |")
    defects = sorted({d for m in methods for d in summary[m]["by_defect"]})
    lines += ["", "Top-1 by defect kind:", "", "| Method | " + " | ".join(defects) + " |", "|---|" + "---|" * len(defects)]
    for method in methods:
        cells = []
        for d in defects:
            s = summary[method]["by_defect"].get(d, {})
            cells.append(f"{s['top1']:.2f} (n={s['n']})" if s.get("n") else "-")
        lines.append(f"| {method} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
