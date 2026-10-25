"""Root-cause GNN: a directed, edge-weighted, edge-conditioned GraphSAGE network in plain PyTorch.

Each layer updates a node from (a) itself, (b) the weighted mean of messages from its predecessors
(callers, endpoints routing to it) and (c) the weighted mean of messages from its successors (callees,
the database it queries). Keeping both directions separate matters: "my callee's queries grow" and "my
caller is the symptom endpoint" are different signals.

Edge conditioning (``edge_dim > 0``): a message is ``h[neighbour] + W_edge · edge_features``, so the
model can use per-call-site evidence such as "this particular call edge is executed N times per request
and that count grows with input size" -- information no single node carries. Checkpoints trained
before edge features existed have ``edge_dim = 0`` and behave exactly as before.

Output: one logit per node; ranking uses a softmax over the symptom's candidate nodes.
Edge weights double as the edge mask for the GNNExplainer-style explanation (ml/explain.py).
"""

from __future__ import annotations

import torch
from torch import nn

from scaletrace.ml.features import EDGE_FEATURE_NAMES, GraphTensors

EDGE_DIM = len(EDGE_FEATURE_NAMES)


def weighted_mean(messages: torch.Tensor, index: torch.Tensor, weight: torch.Tensor, n: int) -> torch.Tensor:
    total = torch.zeros(n, messages.shape[1], dtype=messages.dtype).index_add_(0, index, messages * weight.unsqueeze(1))
    norm = torch.zeros(n, dtype=messages.dtype).index_add_(0, index, weight)
    return total / norm.clamp(min=1e-6).unsqueeze(1)


class DirectedSAGELayer(nn.Module):
    def __init__(self, dim: int, dropout: float, edge_dim: int = 0) -> None:
        super().__init__()
        self.self_linear = nn.Linear(dim, dim)
        self.from_predecessors = nn.Linear(dim, dim, bias=False)
        self.from_successors = nn.Linear(dim, dim, bias=False)
        self.edge_in = nn.Linear(edge_dim, dim, bias=False) if edge_dim else None
        self.edge_out = nn.Linear(edge_dim, dim, bias=False) if edge_dim else None
        self.norm = nn.LayerNorm(dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, h: torch.Tensor, edge_index: torch.Tensor, edge_weight: torch.Tensor, edge_attr: torch.Tensor | None = None) -> torch.Tensor:
        n = h.shape[0]
        if edge_index.numel():
            source, target = edge_index
            to_target, to_source = h[source], h[target]
            if self.edge_in is not None and edge_attr is not None:
                to_target = to_target + self.edge_in(edge_attr)
                to_source = to_source + self.edge_out(edge_attr)
            incoming = weighted_mean(to_target, target, edge_weight, n)
            outgoing = weighted_mean(to_source, source, edge_weight, n)
        else:
            incoming = outgoing = torch.zeros_like(h)
        update = self.self_linear(h) + self.from_predecessors(incoming) + self.from_successors(outgoing)
        return self.norm(h + self.dropout(torch.relu(update)))


class RootCauseGNN(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 64, layers: int = 3, dropout: float = 0.1, edge_dim: int = 0) -> None:
        super().__init__()
        self.config = {"in_dim": in_dim, "hidden": hidden, "layers": layers, "dropout": dropout, "edge_dim": edge_dim}
        self.encoder = nn.Sequential(nn.Linear(in_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
        self.layers = nn.ModuleList(DirectedSAGELayer(hidden, dropout, edge_dim) for _ in range(layers))
        self.head = nn.Sequential(nn.Linear(hidden + in_dim, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_weight: torch.Tensor, edge_attr: torch.Tensor | None = None) -> torch.Tensor:
        h = self.encoder(x)
        for layer in self.layers:
            h = layer(h, edge_index, edge_weight, edge_attr)
        return self.head(torch.cat([h, x], dim=1)).squeeze(-1)


@torch.no_grad()
def to_torch(tensors: GraphTensors, mean: torch.Tensor | None = None, std: torch.Tensor | None = None):
    """(x, edge_index, edge_weight, candidates, labels) -- kept for callers that do not use edge features."""
    x, edge_index, edge_weight, _, candidates, labels = to_torch_full(tensors, mean, std)
    return x, edge_index, edge_weight, candidates, labels


@torch.no_grad()
def to_torch_full(tensors: GraphTensors, mean: torch.Tensor | None = None, std: torch.Tensor | None = None):
    """(x, edge_index, edge_weight, edge_attr, candidates, labels)."""
    x = torch.tensor(tensors.x, dtype=torch.float32)
    if mean is not None and std is not None:
        x = (x - mean) / std
    if tensors.edge_index:
        edge_index = torch.tensor(tensors.edge_index, dtype=torch.long).t().contiguous()
    else:
        edge_index = torch.zeros((2, 0), dtype=torch.long)
    edge_weight = torch.tensor(tensors.edge_weight, dtype=torch.float32)
    if tensors.edge_attr:
        edge_attr = torch.tensor(tensors.edge_attr, dtype=torch.float32)
    else:
        edge_attr = torch.zeros((len(tensors.edge_index), EDGE_DIM), dtype=torch.float32)
    candidates = torch.tensor(tensors.candidates, dtype=torch.long)
    labels = torch.tensor(tensors.labels, dtype=torch.float32) if tensors.labels else None
    return x, edge_index, edge_weight, edge_attr, candidates, labels


def candidate_scores(model, x, edge_index, edge_weight, candidates, edge_attr=None) -> torch.Tensor:
    """Softmax over candidate nodes -> a probability-like score per candidate."""
    logits = model(x, edge_index, edge_weight, edge_attr)
    return torch.softmax(logits[candidates], dim=0)
