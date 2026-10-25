"""Learned baselines that do not use learned message passing.

* ``NodeMLP`` (``mlp``): every node is scored from its own feature vector only. If it matches the GNN,
  the graph adds nothing; if it already solves the task, the dataset is node-locally separable.
* ``NeighborMLP`` (``mlp_hop``): the node's features concatenated with *hand-engineered* one-hop
  summaries -- mean features of predecessors and successors, and the max of each edge feature over
  incoming and outgoing edges -- then an MLP. If it matches the GNN, graph information matters but
  a fixed aggregation is enough; the GNN's learned message passing is then not what makes the difference.

Same features, normalization, loss and training loop as the GNN.
"""

from __future__ import annotations

import torch
from torch import nn

from scaletrace.ml.model import EDGE_DIM, weighted_mean

ARCHITECTURES = ("gnn", "mlp", "mlp_hop")


def _mlp(in_dim: int, hidden: int, layers: int, dropout: float) -> nn.Sequential:
    blocks: list[nn.Module] = [nn.Linear(in_dim, hidden), nn.ReLU()]
    for _ in range(max(0, layers - 1)):
        blocks += [nn.Dropout(dropout), nn.Linear(hidden, hidden), nn.ReLU()]
    blocks.append(nn.Linear(hidden, 1))
    return nn.Sequential(*blocks)


class NodeMLP(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 64, layers: int = 3, dropout: float = 0.1, edge_dim: int = 0) -> None:
        super().__init__()
        self.config = {"in_dim": in_dim, "hidden": hidden, "layers": layers, "dropout": dropout, "edge_dim": edge_dim}
        self.net = _mlp(in_dim, hidden, layers, dropout)

    def forward(self, x, edge_index, edge_weight, edge_attr=None) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def _edge_max(edge_attr: torch.Tensor, index: torch.Tensor, n: int) -> torch.Tensor:
    out = torch.zeros(n, edge_attr.shape[1], dtype=edge_attr.dtype)
    if edge_attr.numel():
        out = out.scatter_reduce(0, index.unsqueeze(1).expand_as(edge_attr), edge_attr, reduce="amax", include_self=True)
    return out


class NeighborMLP(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 64, layers: int = 3, dropout: float = 0.1, edge_dim: int = EDGE_DIM) -> None:
        super().__init__()
        self.config = {"in_dim": in_dim, "hidden": hidden, "layers": layers, "dropout": dropout, "edge_dim": edge_dim}
        self.edge_dim = edge_dim
        self.net = _mlp(3 * in_dim + 2 * edge_dim, hidden, layers, dropout)

    def forward(self, x, edge_index, edge_weight, edge_attr=None) -> torch.Tensor:
        n = x.shape[0]
        if edge_attr is None:
            edge_attr = torch.zeros((edge_index.shape[1], self.edge_dim))
        if edge_index.numel():
            source, target = edge_index
            from_predecessors = weighted_mean(x[source], target, edge_weight, n)
            from_successors = weighted_mean(x[target], source, edge_weight, n)
            in_edges, out_edges = _edge_max(edge_attr, target, n), _edge_max(edge_attr, source, n)
        else:
            from_predecessors = from_successors = torch.zeros_like(x)
            in_edges = out_edges = torch.zeros((n, self.edge_dim))
        return self.net(torch.cat([x, from_predecessors, from_successors, in_edges, out_edges], dim=1)).squeeze(-1)


def make_model(architecture: str, in_dim: int, hidden: int, layers: int, dropout: float, edge_dim: int = 0) -> nn.Module:
    from scaletrace.ml.model import RootCauseGNN

    if architecture == "gnn":
        return RootCauseGNN(in_dim, hidden, layers, dropout, edge_dim)
    if architecture == "mlp":
        return NodeMLP(in_dim, hidden, layers, dropout, edge_dim)
    if architecture == "mlp_hop":
        return NeighborMLP(in_dim, hidden, layers, dropout, edge_dim or EDGE_DIM)
    raise ValueError(f"unknown architecture {architecture!r}; use one of {ARCHITECTURES}")
