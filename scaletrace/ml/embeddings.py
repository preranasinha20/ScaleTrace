"""Code embeddings: turn a function's source code into a fixed-size vector (a GNN node feature).

Two embedders share one interface:

* ``hashed`` (default): a deterministic bag of code tokens -- identifier sub-words, AST node
  types and called method names -- hashed into ``dim`` buckets and L2-normalized. No download,
  instant, reproducible. It is a feature extractor, not a neural network.
* ``codebert`` (optional, needs ``pip install transformers``): mean-pooled hidden states of a
  pretrained code Transformer (default ``microsoft/codebert-base``), reduced to ``dim`` with a
  fixed random projection (Johnson-Lindenstrauss) so the GNN input stays small for small datasets.
  Embeddings are cached on disk by source hash.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import zlib
from pathlib import Path

DEFAULT_DIM = 32
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _subwords(identifier: str) -> list[str]:
    words = []
    for part in identifier.split("_"):
        words.extend(w.lower() for w in _CAMEL.findall(part))
    return words


def code_tokens(source: str) -> list[str]:
    tokens = [w for ident in _IDENTIFIER.findall(source) for w in _subwords(ident)]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return tokens
    for node in ast.walk(tree):
        tokens.append(f"ast:{type(node).__name__}")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name:
                tokens.append(f"call:{name}")
    return tokens


class HashedCodeEmbedder:
    name = "hashed"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def embed(self, sources: list[str]) -> list[list[float]]:
        vectors = []
        for source in sources:
            vector = [0.0] * self.dim
            for token in code_tokens(source) if source else []:
                digest = zlib.crc32(token.encode("utf-8"))
                sign = 1.0 if (digest >> 31) & 1 else -1.0
                vector[digest % self.dim] += sign
            norm = math.sqrt(sum(v * v for v in vector))
            vectors.append([v / norm for v in vector] if norm else vector)
        return vectors


class TransformerCodeEmbedder:
    """Pretrained code Transformer embeddings (CPU), projected to ``dim`` dimensions."""

    name = "codebert"

    def __init__(self, dim: int = DEFAULT_DIM, model_name: str = "microsoft/codebert-base", cache_dir: str | Path | None = None) -> None:
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("the 'codebert' embedder needs: pip install transformers") from exc
        self.dim = dim
        self.model_name = model_name
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name).eval()
        hidden = self.model.config.hidden_size
        generator = torch.Generator().manual_seed(1234)
        self.projection = torch.randn(hidden, dim, generator=generator) / math.sqrt(dim)
        self.cache_path = Path(cache_dir or ".scaletrace_cache") / f"emb_{model_name.replace('/', '_')}_{dim}.json"
        self._cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def embed(self, sources: list[str]) -> list[list[float]]:
        torch = self._torch
        keys = [hashlib.sha1(s.encode("utf-8")).hexdigest() for s in sources]
        missing = [(k, s) for k, s in zip(keys, sources) if k not in self._cache]
        with torch.no_grad():
            for start in range(0, len(missing), 16):
                batch = missing[start:start + 16]
                encoded = self.tokenizer([s or " " for _, s in batch], padding=True, truncation=True, max_length=256, return_tensors="pt")
                hidden = self.model(**encoded).last_hidden_state
                mask = encoded["attention_mask"].unsqueeze(-1).float()
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1.0)
                projected = torch.nn.functional.normalize(pooled @ self.projection, dim=-1)
                for (key, _), vector in zip(batch, projected.tolist()):
                    self._cache[key] = vector
        if missing:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
        return [self._cache[k] for k in keys]


def make_embedder(name: str, dim: int = DEFAULT_DIM):
    if name == "hashed":
        return HashedCodeEmbedder(dim)
    if name == "codebert":
        return TransformerCodeEmbedder(dim)
    raise ValueError(f"unknown embedder {name!r} (use 'hashed' or 'codebert')")
