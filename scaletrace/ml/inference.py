"""Using a trained GNN inside `scaletrace diagnose` (as the Backtracker's ranker)."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import torch

from scaletrace.analyzer.project import ProjectIndex
from scaletrace.ml.embeddings import make_embedder
from scaletrace.ml.explain import explain_candidate
from scaletrace.ml.features import FeatureBuilder
from scaletrace.ml.model import to_torch_full
from scaletrace.ml.train import load_checkpoint
from scaletrace.schemas.diagnosis import Symptom
from scaletrace.schemas.runtime import RuntimeReport


def load_ranker(path: str | Path, project: ProjectIndex, runtime: RuntimeReport | None):
    """Returns (ranker, explainer) closures bound to one project and runtime report."""
    model, checkpoint = load_checkpoint(path)
    builder = FeatureBuilder(checkpoint["groups"], make_embedder(checkpoint["embedder"], checkpoint["code_dim"]), checkpoint["code_dim"])
    use_runtime = "runtime" in checkpoint["groups"]

    def _tensors(graph: nx.MultiDiGraph, symptom: Symptom):
        if symptom.endpoint is None:
            return None
        kind = symptom.kind if symptom.kind in ("query_growth", "latency_growth") else "query_growth"
        return builder.build(graph, symptom.endpoint, kind, project, runtime if use_runtime else None)

    def ranker(graph: nx.MultiDiGraph, symptom: Symptom, nodes: list[str]) -> dict[str, float]:
        tensors = _tensors(graph, symptom)
        if tensors is None or not tensors.candidates:
            return {}
        x, edge_index, edge_weight, edge_attr, candidates, _ = to_torch_full(tensors, checkpoint["mean"], checkpoint["std"])
        with torch.no_grad():
            scores = torch.softmax(model(x, edge_index, edge_weight, edge_attr)[candidates], dim=0).tolist()
        by_node = {tensors.nodes[i]: s for i, s in zip(tensors.candidates, scores)}
        return {n: by_node[n] for n in nodes if n in by_node}

    def explainer(graph: nx.MultiDiGraph, symptom: Symptom, node: str) -> dict | None:
        tensors = _tensors(graph, symptom)
        if tensors is None or node not in tensors.nodes:
            return None
        names = checkpoint["feature_names"]
        return explain_candidate(model, tensors, node, names, checkpoint["mean"], checkpoint["std"])

    return ranker, explainer
