"""Training the root-cause GNN with a listwise ranking loss.

For each sample (one symptom), the loss is the negative log of the softmax probability mass on
the true root-cause candidates: ``-log(sum_{pos} softmax(logits[candidates])[pos])``. This directly
optimizes "rank the root cause first" (Top-1 / MRR) rather than independent node classification.

Robustness: with ``runtime_dropout`` > 0, the runtime feature group is zeroed for a random fraction
of training samples, so a combined model still behaves sensibly when runtime evidence is missing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import torch

from scaletrace.ml.dataset import Sample
from scaletrace.ml.baselines import NeighborMLP, NodeMLP, make_model
from scaletrace.ml.model import EDGE_DIM, RootCauseGNN, to_torch_full

EDGE_RUNTIME_COLUMNS = [6, 7, 8]  # edge:observed, edge:observed_call_growth, edge:log_observed_calls

CHECKPOINT_VERSION = 1


@dataclass
class TrainConfig:
    hidden: int = 64
    layers: int = 3
    dropout: float = 0.1
    lr: float = 3e-3
    weight_decay: float = 1e-4
    epochs: int = 150
    batch_size: int = 16
    patience: int = 30
    runtime_dropout: float = 0.15
    seed: int = 0
    architecture: str = "gnn"  # "gnn", "mlp" (node-only) or "mlp_hop" (engineered 1-hop), see ml/baselines.py
    edge_features: bool = True  # edge-conditioned messages (generator v5 experiments); False = Phase 1 model


@dataclass
class TrainResult:
    model: torch.nn.Module
    mean: torch.Tensor
    std: torch.Tensor
    history: list[dict] = field(default_factory=list)
    best_epoch: int = 0


def normalization(samples: list[Sample]) -> tuple[torch.Tensor, torch.Tensor]:
    stacked = torch.cat([torch.tensor(s.tensors.x, dtype=torch.float32) for s in samples], dim=0)
    mean = stacked.mean(0)
    std = stacked.std(0).clamp(min=1e-3)
    # Binary / one-hot columns are left unscaled so "0 = absent" keeps its meaning.
    binary = ((stacked == 0) | (stacked == 1)).all(0)
    mean[binary] = 0.0
    std[binary] = 1.0
    return mean, std


def collate(items: list[tuple]) -> tuple:
    """Disjoint union of several graphs: one forward pass, identical results to separate passes
    (message passing never crosses graphs; LayerNorm is per node)."""
    with_edges = len(items[0]) == 6
    xs, edges, weights, attrs, candidate_sets, labels = [], [], [], [], [], []
    offset = 0
    for item in items:
        if with_edges:
            x, edge_index, edge_weight, edge_attr, candidates, label = item
            attrs.append(edge_attr)
        else:
            x, edge_index, edge_weight, candidates, label = item
        xs.append(x)
        edges.append(edge_index + offset)
        weights.append(edge_weight)
        candidate_sets.append(candidates + offset)
        labels.append(label)
        offset += x.shape[0]
    joined = (torch.cat(xs), torch.cat(edges, dim=1), torch.cat(weights))
    if with_edges:
        return (*joined, torch.cat(attrs), candidate_sets, torch.cat(labels))
    return (*joined, candidate_sets, torch.cat(labels))


def listwise_loss(logits: torch.Tensor, candidates: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
    candidate_logits = logits[candidates]
    positives = labels[candidates] > 0
    log_probs = torch.log_softmax(candidate_logits, dim=0)
    return -torch.logsumexp(log_probs[positives], dim=0)


def ranks_for(model: torch.nn.Module, samples: list[Sample], mean, std) -> list[float | None]:
    """Expected rank (1 = best, ties averaged) of the best-ranked true root cause per sample."""
    model.eval()
    ranks: list[float | None] = []
    with torch.no_grad():
        for sample in samples:
            x, edge_index, edge_weight, edge_attr, candidates, labels = to_torch_full(sample.tensors, mean, std)
            if not sample.has_positive or candidates.numel() == 0:
                ranks.append(None)
                continue
            scores = model(x, edge_index, edge_weight, edge_attr)[candidates]
            positives = labels[candidates] > 0
            ranks.append(expected_rank(scores.tolist(), positives.tolist()))
    return ranks


def expected_rank(scores: list[float], positives: list[bool]) -> float:
    best = max(s for s, p in zip(scores, positives) if p)
    higher = sum(1 for s in scores if s > best)
    ties = sum(1 for s in scores if s == best)
    return higher + (ties + 1) / 2


def mrr(ranks: list[float | None]) -> float:
    return sum((1.0 / r) if r else 0.0 for r in ranks) / len(ranks) if ranks else 0.0


def _runtime_columns(feature_names: list[str]) -> list[int]:
    return [i for i, name in enumerate(feature_names) if name.startswith("runtime:")]


def train_model(
    train: list[Sample],
    validation: list[Sample],
    feature_names: list[str],
    config: TrainConfig | None = None,
    log=print,
) -> TrainResult:
    config = config or TrainConfig()
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    usable = [s for s in train if s.has_positive and s.tensors.candidates]
    if not usable:
        raise ValueError("no training samples with a reachable root cause")
    mean, std = normalization(usable)
    model = make_model(config.architecture, len(feature_names), config.hidden, config.layers, config.dropout,
                       EDGE_DIM if config.edge_features else 0)
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.lr, weight_decay=config.weight_decay)
    runtime_cols = _runtime_columns(feature_names)
    tensors = [to_torch_full(s.tensors, mean, std) for s in usable]

    best_state, best_score, best_epoch, history = None, -1.0, 0, []
    for epoch in range(1, config.epochs + 1):
        model.train()
        order = list(range(len(tensors)))
        random.shuffle(order)
        total = 0.0
        for start in range(0, len(order), config.batch_size):
            optimizer.zero_grad()
            items = []
            for i in order[start:start + config.batch_size]:
                x, edge_index, edge_weight, edge_attr, candidates, labels = tensors[i]
                if runtime_cols and random.random() < config.runtime_dropout:
                    x, edge_attr = x.clone(), edge_attr.clone()
                    x[:, runtime_cols] = 0.0
                    edge_attr[:, EDGE_RUNTIME_COLUMNS] = 0.0
                items.append((x, edge_index, edge_weight, edge_attr, candidates, labels))
            x, edge_index, edge_weight, edge_attr, candidate_sets, labels = collate(items)
            logits = model(x, edge_index, edge_weight, edge_attr)
            batch_loss = sum(listwise_loss(logits, c, labels) for c in candidate_sets) / len(candidate_sets)
            batch_loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            total += batch_loss.item()

        score = mrr(ranks_for(model, validation or usable, mean, std))
        history.append({"epoch": epoch, "loss": total, "validation_mrr": score})
        if score > best_score + 1e-6:
            best_score, best_epoch = score, epoch
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        elif epoch - best_epoch >= config.patience:
            break
        if epoch % 25 == 0:
            log(f"  epoch {epoch:4d}  loss {total:8.4f}  validation MRR {score:.3f}")
    model.load_state_dict(best_state)
    model.eval()
    return TrainResult(model, mean, std, history, best_epoch)


def save_checkpoint(path: str | Path, result: TrainResult, meta: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "version": CHECKPOINT_VERSION,
            "architecture": "mlp_hop" if isinstance(result.model, NeighborMLP) else ("mlp" if isinstance(result.model, NodeMLP) else "gnn"),
            "model_config": result.model.config,
            "state_dict": result.model.state_dict(),
            "mean": result.mean,
            "std": result.std,
            "best_epoch": result.best_epoch,
            "history": result.history,
            **meta,
        },
        path,
    )
    return path


def load_checkpoint(path: str | Path) -> tuple[torch.nn.Module, dict]:
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    if checkpoint.get("version") != CHECKPOINT_VERSION:
        raise ValueError(f"unsupported checkpoint version in {path}")
    config = checkpoint["model_config"]
    model = make_model(checkpoint.get("architecture", "gnn"), config["in_dim"], config["hidden"], config["layers"], config["dropout"],
                       config.get("edge_dim", 0))
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return model, checkpoint
