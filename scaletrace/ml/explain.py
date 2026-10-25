"""GNNExplainer-style explanations for the root-cause GNN (Ying et al., 2019, adapted to ranking).

For a target candidate we learn a soft mask over edges (scaling edge weights) and over feature
columns (shared across nodes) that keeps the target's *ranking* among candidates while using as
few edges/features as possible:

    loss = -log softmax(logits[candidates])[target]
           + a * mean(edge_mask) + b * mean(feature_mask)
           + c * entropy(edge_mask) + c * entropy(feature_mask)

High-mask edges and features are the evidence the model relied on. This explains the *model*;
it does not prove the code caused the symptom.
"""

from __future__ import annotations

import torch

from scaletrace.ml.features import GraphTensors
from scaletrace.ml.model import RootCauseGNN, to_torch_full


def _entropy(mask: torch.Tensor) -> torch.Tensor:
    mask = mask.clamp(1e-6, 1 - 1e-6)
    return (-(mask * mask.log()) - (1 - mask) * (1 - mask).log()).mean()


def explain_candidate(
    model: RootCauseGNN,
    tensors: GraphTensors,
    target_node: str,
    feature_names: list[str],
    mean: torch.Tensor,
    std: torch.Tensor,
    steps: int = 200,
    lr: float = 0.05,
    edge_size: float = 0.05,
    feature_size: float = 0.05,
    entropy_weight: float = 0.05,
    top_k: int = 8,
    seed: int = 0,
) -> dict:
    torch.manual_seed(seed)
    x, edge_index, edge_weight, edge_attr, candidates, _ = to_torch_full(tensors, mean, std)
    target = tensors.nodes.index(target_node)
    position = (candidates == target).nonzero()
    if position.numel() == 0:
        return {"target": target_node, "top_edges": [], "top_features": [], "note": "target is not a candidate"}
    position = int(position[0])

    for parameter in model.parameters():
        parameter.requires_grad_(False)
    edge_logits = torch.full((edge_weight.shape[0],), 1.0, requires_grad=True)
    feature_logits = torch.full((x.shape[1],), 1.0, requires_grad=True)
    optimizer = torch.optim.Adam([edge_logits, feature_logits], lr=lr)
    for _ in range(steps):
        optimizer.zero_grad()
        edge_mask, feature_mask = torch.sigmoid(edge_logits), torch.sigmoid(feature_logits)
        logits = model(x * feature_mask, edge_index, edge_weight * edge_mask, edge_attr)
        loss = -torch.log_softmax(logits[candidates], dim=0)[position]
        loss = loss + edge_size * edge_mask.mean() + feature_size * feature_mask.mean()
        loss = loss + entropy_weight * (_entropy(edge_mask) + _entropy(feature_mask))
        loss.backward()
        optimizer.step()
    for parameter in model.parameters():
        parameter.requires_grad_(True)

    edge_mask = torch.sigmoid(edge_logits).detach()
    feature_mask = torch.sigmoid(feature_logits).detach()
    edges = sorted(
        (
            {
                "source": tensors.nodes[s],
                "target": tensors.nodes[t],
                "kind": kind,
                "importance": float(edge_mask[i]),
            }
            for i, ((s, t), kind) in enumerate(zip(tensors.edge_index, tensors.edge_kinds))
        ),
        key=lambda e: -e["importance"],
    )
    groups: dict[str, float] = {}
    for name, value in zip(feature_names, feature_mask.tolist()):
        group = name.split(":", 1)[0]
        groups[group] = max(groups.get(group, 0.0), value)
    features = sorted(
        ({"feature": name, "importance": float(v)} for name, v in zip(feature_names, feature_mask.tolist())),
        key=lambda f: -f["importance"],
    )
    return {
        "target": target_node,
        "method": "GNNExplainer-style edge and feature masks",
        "top_edges": edges[:top_k],
        "top_features": features[:top_k],
        "feature_groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
    }
