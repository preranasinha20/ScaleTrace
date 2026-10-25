"""ML components: generator validity, features, GNN forward/training, explanation (fast, static data only)."""

from __future__ import annotations

import ast
import json

import pytest

torch = pytest.importorskip("torch")

from scaletrace.ml.dataset import build_samples, find_apps, load_bundle  # noqa: E402
from scaletrace.ml.embeddings import HashedCodeEmbedder, code_tokens  # noqa: E402
from scaletrace.ml.explain import explain_candidate  # noqa: E402
from scaletrace.ml.features import VARIANT_GROUPS, FeatureBuilder  # noqa: E402
from scaletrace.ml.generator import DEFECT_KINDS, ROOTCAUSE_FILE, AppGenerator  # noqa: E402
from scaletrace.ml.model import RootCauseGNN, to_torch  # noqa: E402
from scaletrace.ml.train import TrainConfig, expected_rank, load_checkpoint, save_checkpoint, train_model  # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("generated")
    for i in range(6):
        AppGenerator(seed=i, style="A" if i % 2 else "B").generate(root / f"app_{i}", n_defects=4, n_clean=2, kinds=DEFECT_KINDS)
    return root


def test_generated_apps_parse_and_label_existing_functions(generated):
    apps = find_apps([generated])
    assert len(apps) == 6
    for app in apps:
        for source in app.rglob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"))
        bundle = load_bundle(app)
        labels = json.loads((app / ROOTCAUSE_FILE).read_text(encoding="utf-8"))
        for symptom in labels["symptoms"]:
            for func_id in symptom["root_cause_functions"]:
                assert func_id in bundle.static.project.functions, func_id
            assert f"endpoint:{symptom['endpoint']}" in bundle.static_graph


def test_generator_is_deterministic(tmp_path):
    AppGenerator(seed=3).generate(tmp_path / "a")
    AppGenerator(seed=3).generate(tmp_path / "b")
    for source in (tmp_path / "a").rglob("*.py"):
        twin = tmp_path / "b" / source.relative_to(tmp_path / "a")
        assert source.read_text() == twin.read_text()


def test_hashed_embedder_is_normalized_and_stable():
    embedder = HashedCodeEmbedder(16)
    [a, b] = embedder.embed(["def f(db):\n    return db.query(X).all()\n", ""])
    assert sum(v * v for v in a) == pytest.approx(1.0)
    assert b == [0.0] * 16
    assert embedder.embed(["def f(db):\n    return db.query(X).all()\n"])[0] == a
    assert "call:query" in code_tokens("db.query(X)")


def test_static_samples_and_feature_shapes(generated):
    builder = FeatureBuilder(VARIANT_GROUPS["static"], HashedCodeEmbedder(8), 8)
    samples = [s for app in find_apps([generated]) for s in build_samples(load_bundle(app), builder, use_runtime=False)]
    assert samples
    for sample in samples:
        assert all(len(row) == len(builder.names) for row in sample.tensors.x)
        assert sample.tensors.candidates
    # Static graphs resolve the generated call structure, so most root causes are reachable candidates.
    assert sum(s.has_positive for s in samples) / len(samples) > 0.8


def test_gnn_trains_saves_and_explains(generated, tmp_path):
    builder = FeatureBuilder(VARIANT_GROUPS["static"], HashedCodeEmbedder(8), 8)
    samples = [s for app in find_apps([generated]) for s in build_samples(load_bundle(app), builder, use_runtime=False)]
    result = train_model(samples, [], builder.names, TrainConfig(epochs=15, hidden=16, layers=2, patience=50), log=lambda *_: None)
    assert result.best_epoch >= 1

    path = save_checkpoint(tmp_path / "m.pt", result, {"variant": "static", "groups": builder.groups, "embedder": "hashed", "code_dim": 8, "feature_names": builder.names})
    model, checkpoint = load_checkpoint(path)
    sample = next(s for s in samples if s.has_positive)
    x, edge_index, edge_weight, candidates, _ = to_torch(sample.tensors, checkpoint["mean"], checkpoint["std"])
    assert torch.allclose(model(x, edge_index, edge_weight), result.model(x, edge_index, edge_weight))

    target = sample.tensors.nodes[sample.tensors.candidates[0]]
    explanation = explain_candidate(model, sample.tensors, target, builder.names, checkpoint["mean"], checkpoint["std"], steps=20)
    assert explanation["top_edges"] and explanation["top_features"]
    assert all(0.0 <= e["importance"] <= 1.0 for e in explanation["top_edges"])


def test_model_handles_graph_without_edges():
    model = RootCauseGNN(in_dim=5, hidden=8, layers=2)
    out = model(torch.randn(3, 5), torch.zeros((2, 0), dtype=torch.long), torch.zeros(0))
    assert out.shape == (3,)


def test_expected_rank_handles_ties():
    assert expected_rank([0.9, 0.1, 0.5], [True, False, False]) == 1
    assert expected_rank([0.5, 0.5, 0.1], [True, False, False]) == 1.5
    assert expected_rank([0.1, 0.9], [True, False]) == 2
