"""Workstream B acceptance tests: shortcut checks, generator v4 properties, label verification,
validation gate, batching equivalence, node-only baseline checkpoints."""

from __future__ import annotations

import ast
import json
import random

import pytest

torch = pytest.importorskip("torch")

from scaletrace.benchmark import MANIFEST_FILE, TestSetAccessError  # noqa: E402
from scaletrace.ml.dataset import Sample, build_samples, find_apps, load_bundle  # noqa: E402
from scaletrace.ml.embeddings import HashedCodeEmbedder  # noqa: E402
from scaletrace.ml.experiment import NODE_ONLY, PRIMARY, evaluate_gate, method_stats  # noqa: E402
from scaletrace.ml.evaluate import RankRecord  # noqa: E402
from scaletrace.ml.features import VARIANT_GROUPS, FeatureBuilder, GraphTensors  # noqa: E402
from scaletrace.ml.generator import DEFECT_KINDS, ROOTCAUSE_FILE, AppGenerator  # noqa: E402
from scaletrace.ml.model import RootCauseGNN, to_torch  # noqa: E402
from scaletrace.ml.shortcuts import (  # noqa: E402
    Check,
    ShortcutReport,
    fingerprint_check,
    make_renamer,
    single_feature_check,
)
from scaletrace.ml.train import TrainConfig, collate, load_checkpoint, save_checkpoint, train_model  # noqa: E402
from scaletrace.ml.verify import verify_app  # noqa: E402
from scaletrace.schemas.runtime import EndpointRuntime, Measurement, RuntimeReport  # noqa: E402


# --- shortcut checks on hand-built samples ------------------------------------------------------

def _sample(rows: list[list[float]], root: int) -> Sample:
    tensors = GraphTensors(
        nodes=[f"n{i}" for i in range(len(rows))], x=rows, edge_index=[], edge_weight=[], edge_kinds=[],
        candidates=list(range(len(rows))), labels=[1.0 if i == root else 0.0 for i in range(len(rows))],
    )
    return Sample("app", "GET /x", "query_growth", "db_query", tensors)


NAMES = ["structure:loop_count", "structure:n_db_ops", "runtime:query_growth"]


def test_single_feature_check_fails_on_a_giveaway_feature():
    rng = random.Random(0)
    samples = []
    for _ in range(40):
        root = rng.randrange(4)
        samples.append(_sample([[1.0 if i == root else 0.0, rng.random(), rng.random()] for i in range(4)], root))
    check = single_feature_check(samples, NAMES)
    assert check.status == "fail" and "loop_count" in check.detail


def test_single_feature_check_ignores_runtime_features_and_passes_on_noise():
    rng = random.Random(1)
    samples = []
    for _ in range(40):
        root = rng.randrange(4)
        # The runtime column is a perfect signal, but it is legitimate evidence, not a shortcut.
        samples.append(_sample([[rng.random(), rng.random(), 1.0 if i == root else 0.0] for i in range(4)], root))
    assert single_feature_check(samples, NAMES).status == "pass"


def test_fingerprint_check():
    unique = [_sample([[3.0, 2.0, 0.0], [1.0, 1.0, 0.0]], 0) for _ in range(5)]
    assert fingerprint_check(unique, unique, NAMES).status == "fail"
    shared = [_sample([[1.0, 1.0, 0.0], [1.0, 1.0, 0.0]], 0) for _ in range(5)]
    assert fingerprint_check(shared, shared, NAMES).status == "pass"


def test_report_verdict_warnings_do_not_fail():
    report = ShortcutReport([Check("a", "pass", 0.1, 0.9, ""), Check("b", "warn", 1.0, None, "")])
    assert report.verdict == "PASS"
    report.checks.append(Check("c", "fail", 1.0, 0.9, ""))
    assert report.verdict == "FAIL"


def test_renamer_replaces_project_names_but_keeps_api_names(tmp_path):
    AppGenerator(seed=3).generate(tmp_path / "app")
    bundle = load_bundle(tmp_path / "app")
    info = next(i for i in bundle.static.project.iter_functions() if ".query(" in i.source)
    renamed = make_renamer(5)(info.source, bundle.static.project)
    ast.parse(renamed)
    assert info.scope.qualname.split(".")[-1] not in renamed
    assert ".query(" in renamed
    assert make_renamer(5)(info.source, bundle.static.project) == renamed  # deterministic


# --- generator v4 ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("v4")
    for i in range(24):
        AppGenerator(seed=i, style="A" if i % 3 else "B").generate(root / f"app_{i:02d}")
    return root


def test_v4_apps_parse_labels_resolve_and_are_deterministic(corpus, tmp_path):
    labels_total = resolved = 0
    for app in find_apps([corpus]):
        for source in app.rglob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"))
        bundle = load_bundle(app)
        for symptom in bundle.labels["symptoms"]:
            labels_total += 1
            resolved += all(f in bundle.static.project.functions for f in symptom["root_cause_functions"] + symptom["decoy_functions"])
    assert labels_total and resolved / labels_total >= 0.95
    AppGenerator(seed=7).generate(tmp_path / "a")
    AppGenerator(seed=7).generate(tmp_path / "b")
    for source in (tmp_path / "a").rglob("*.py"):
        assert source.read_text() == (tmp_path / "b" / source.relative_to(tmp_path / "a")).read_text()


def test_v4_covers_kinds_depths_decoys_and_loop_forms(corpus):
    labels = [s for app in find_apps([corpus]) for s in json.loads((app / ROOTCAUSE_FILE).read_text())["symptoms"]]
    assert {s["defect"] for s in labels} == set(DEFECT_KINDS)
    assert {s["depth"] for s in labels} == {0, 1, 2, 3}
    assert {s["loop_form"] for s in labels} >= {"for", "comprehension", "while"}
    assert any(s["decoy_functions"] for s in labels)
    assert any(s["class_based"] for s in labels)


def test_v4_has_no_structural_fingerprint_shortcut(corpus):
    builder = FeatureBuilder(VARIANT_GROUPS["static"], HashedCodeEmbedder(8), 8)
    apps = find_apps([corpus])
    samples = [s for app in apps for s in build_samples(load_bundle(app), builder, use_runtime=False)]
    first, second = samples[: len(samples) // 2], samples[len(samples) // 2:]
    assert fingerprint_check(first, second, builder.names).status == "pass"
    assert single_feature_check(samples, builder.names).status == "pass"


# --- label verification -------------------------------------------------------------------------

def _runtime(endpoint: str, queries: list[float], decoy: str, decoy_queries: list[float]) -> RuntimeReport:
    return RuntimeReport(
        scaletrace_version="t", repository="x", app="app.main:app", generated_at="now", status="completed",
        instrumentation={"sqlalchemy_queries": True, "call_tracing": True},
        endpoints=[EndpointRuntime(endpoint=endpoint, request_name="r", measurements=[
            Measurement(size=s, requests=2, errors=0, queries_per_request=q, latency_ms_mean=1.0,
                        queries_by_function_per_request={decoy: dq}, function_calls_per_request={decoy: 1.0})
            for s, q, dq in zip([10, 40, 160], queries, decoy_queries)
        ])],
    )


def test_verify_labels_accepts_growth_rejects_constant(tmp_path):
    app = tmp_path / "gen"
    labels = AppGenerator(seed=11).generate(app, n_defects=1, n_clean=0, kinds=["db_query"])
    symptom = labels["symptoms"][0]
    decoy = symptom["decoy_functions"][0] if symptom["decoy_functions"] else "app.services:none"
    cache = app / ".scaletrace"
    cache.mkdir()
    (cache / "runtime.json").write_text(_runtime(symptom["endpoint"], [12, 42, 162], decoy, [3, 3, 3]).model_dump_json())
    assert verify_app(app)["verified"] == 1
    stored = json.loads((app / ROOTCAUSE_FILE).read_text())["symptoms"][0]["verification"]
    assert stored["runtime"] == "verified"
    (cache / "runtime.json").write_text(_runtime(symptom["endpoint"], [12, 12, 12], decoy, [3, 30, 120]).model_dump_json())
    counts = verify_app(app)
    assert counts["rejected"] == 1 and counts["decoys_growing"] == (1 if symptom["decoy_functions"] else 0)


def test_verify_labels_refuses_benchmark_apps(tmp_path):
    app = tmp_path / "team-app"
    AppGenerator(seed=1).generate(app)
    (app / MANIFEST_FILE).write_text(json.dumps({"app_id": "team-app", "kind": "bad", "origin": {"source": "team"}}))
    with pytest.raises(TestSetAccessError):
        verify_app(app)


# --- gate logic -----------------------------------------------------------------------------------

def _records(method: str, rr_by_app: dict[str, list[float]], seeds=(0, 1, 2)) -> list[RankRecord]:
    out = []
    for seed in seeds:
        for app, values in rr_by_app.items():
            for i, rr in enumerate(values):
                out.append(RankRecord(method, app, f"GET /{i}", "db_query", 1.0 / rr if rr else None, 5, seed))
    return out


def _split(primary_rr: float, baseline_rr: float, mlp_rr: float) -> dict:
    apps = [f"a{i}" for i in range(20)]
    records = _records(PRIMARY, {a: [primary_rr] * 4 for a in apps})
    records += _records(NODE_ONLY, {a: [mlp_rr] * 4 for a in apps})
    records += _records("rules_only", {a: [baseline_rr] * 4 for a in apps}, seeds=(None,))
    return {"methods": {m: method_stats(records, m, 500) for m in (PRIMARY, NODE_ONLY, "rules_only")}}


def test_gate_passes_only_when_every_criterion_passes():
    good = _split(1.0, 0.5, 0.5)
    passing = ShortcutReport([Check("x", "pass", 0.1, 0.9, "")])
    assert evaluate_gate({"style_holdout": good, "leave_one_kind_out": good}, passing)["validated"] is True

    failing_suite = ShortcutReport([Check("x", "fail", 1.0, 0.9, "")])
    assert evaluate_gate({"style_holdout": good, "leave_one_kind_out": good}, failing_suite)["validated"] is False

    tie = _split(0.5, 0.5, 0.25)
    gate = evaluate_gate({"style_holdout": good, "leave_one_kind_out": tie}, passing)
    assert gate["validated"] is False
    assert not next(c for c in gate["criteria"] if c["id"] == "C2/leave_one_kind_out")["passed"]

    assert evaluate_gate({"style_holdout": good}, passing)["validated"] is False  # missing split


def test_method_stats_bootstrap_brackets_point_estimate():
    records = _records(PRIMARY, {f"a{i}": [1.0, 2.0, 4.0] for i in range(10)})
    stats = method_stats(records, PRIMARY, 300)
    low, high = stats["ci95"]
    assert low <= stats["mrr_pooled"] <= high
    assert stats["n_apps"] == 10 and stats["mrr_sd"] == pytest.approx(0.0)


# --- training mechanics --------------------------------------------------------------------------

def test_collate_matches_separate_forward_passes():
    torch.manual_seed(0)
    model = RootCauseGNN(in_dim=4, hidden=8, layers=2).eval()
    graphs = []
    for n in (3, 5):
        x = torch.randn(n, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]]) if n == 3 else torch.tensor([[0, 1, 3], [1, 2, 4]])
        graphs.append((x, edge_index, torch.ones(edge_index.shape[1]), torch.arange(n), torch.zeros(n)))
    x, edge_index, edge_weight, candidates, _ = collate(graphs)
    joint = model(x, edge_index, edge_weight)
    separate = torch.cat([model(g[0], g[1], g[2]) for g in graphs])
    assert torch.allclose(joint, separate, atol=1e-6)
    assert [c.tolist() for c in candidates] == [[0, 1, 2], [3, 4, 5, 6, 7]]


def test_node_only_mlp_checkpoint_round_trip(corpus, tmp_path):
    builder = FeatureBuilder(VARIANT_GROUPS["static"], HashedCodeEmbedder(8), 8)
    samples = [s for app in find_apps([corpus])[:6] for s in build_samples(load_bundle(app), builder, use_runtime=False)]
    result = train_model(samples, [], builder.names, TrainConfig(epochs=3, hidden=8, layers=2, architecture="mlp"), log=lambda *_: None)
    path = save_checkpoint(tmp_path / "mlp.pt", result, {"variant": "static", "groups": builder.groups, "embedder": "hashed",
                                                        "code_dim": 8, "feature_names": builder.names})
    model, checkpoint = load_checkpoint(path)
    assert checkpoint["architecture"] == "mlp"
    x, edge_index, edge_weight, _, _ = to_torch(samples[0].tensors, checkpoint["mean"], checkpoint["std"])
    assert torch.allclose(model(x, edge_index, edge_weight), result.model(x, edge_index, edge_weight))
