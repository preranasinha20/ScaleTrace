"""Phase 2 tests: register import, fetch (offline), screening, final evaluation, generator v5,
edge-conditioned models, edge-aware heuristic."""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from scaletrace.benchmark import MANIFEST_FILE, TestSetAccessError, load_manifest  # noqa: E402
from scaletrace.benchmark import fetch as fetch_module  # noqa: E402
from scaletrace.benchmark.final import FinalEvaluationError, run_final  # noqa: E402
from scaletrace.benchmark.loader import load_index, save_index  # noqa: E402
from scaletrace.benchmark.register import import_registers  # noqa: E402
from scaletrace.benchmark.screen import screen_candidate  # noqa: E402
from scaletrace.benchmark.split import build_index, lock_index  # noqa: E402
from scaletrace.ml.baselines import NeighborMLP  # noqa: E402
from scaletrace.ml.dataset import load_bundle  # noqa: E402
from scaletrace.ml.generator import ROOTCAUSE_FILE, SHARED_KINDS, AppGenerator  # noqa: E402
from scaletrace.ml.model import EDGE_DIM, RootCauseGNN  # noqa: E402
from scaletrace.ml.train import TrainConfig, collate, load_checkpoint, save_checkpoint, train_model  # noqa: E402
from scaletrace.ml.verify import verify_app  # noqa: E402
from scaletrace.rules import load_rulebook  # noqa: E402
from scaletrace.schemas.runtime import EndpointRuntime, Measurement, ObservedEdge, RuntimeReport  # noqa: E402
from tests.conftest import EXAMPLES  # noqa: E402

GOOD_COLUMNS = ["Candidate_ID", "Repository", "GitHub_URL", "Licence", "Licence_Status", "Framework", "Decision",
                "Commit_SHA", "Second_Pass_Commit_SHA", "Second_Pass_Decision", "Second_Pass_License_Evidence",
                "Rule_IDs_Checked", "Evidence_URLs", "Date_Checked"]
BAD_COLUMNS = ["Candidate_ID", "Repository", "GitHub_URL", "Licence", "Licence_Status", "Framework", "Decision",
               "Commit_SHA", "Rule_IDs_Checked", "Evidence_Status", "Findings_and_Evidence", "Evidence_URLs", "Date_Checked"]


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    path.write_bytes(buffer.getvalue().encode("cp1252"))  # the team's export encoding
    return path


@pytest.fixture
def registers(tmp_path):
    good = _write_csv(tmp_path / "good.csv", GOOD_COLUMNS, [
        {"Candidate_ID": "C001", "Repository": "a/one", "GitHub_URL": "https://github.com/a/one", "Licence": "MIT",
         "Framework": "FastAPI", "Commit_SHA": "Not captured", "Second_Pass_Decision": "HOLD – licence unresolved",
         "Second_Pass_License_Evidence": "Formal LICENSE file visible; MIT indicated",
         "Rule_IDs_Checked": "DB-001, MEM-001 checked"},
        {"Candidate_ID": "C002", "Repository": "b/two", "GitHub_URL": "https://github.com/b/two", "Licence": "MIT (reported)",
         "Framework": "FastAPI", "Commit_SHA": "0123456789abcdef0123456789abcdef01234567",
         "Second_Pass_Decision": "NEEDS FULL SOURCE REVIEW – not accepted"},
    ])
    bad = _write_csv(tmp_path / "bad.csv", BAD_COLUMNS, [
        {"Candidate_ID": "BAD-CAND-001", "Repository": "c/three", "GitHub_URL": "https://github.com/c/three",
         "Framework": "FastAPI", "Decision": "retain_candidate", "Commit_SHA": "Not captured", "Rule_IDs_Checked": "MEM-001",
         "Evidence_Status": "Source-confirmed MEM-001 pattern; runtime not verified",
         "Evidence_URLs": "https://github.com/c/three/blob/main/app/main.py#L10-L12 ; https://github.com/c/three/blob/main/app/x.py#L5"},
        {"Candidate_ID": "BAD-CAND-002", "Repository": "d/four", "GitHub_URL": "https://github.com/d/four",
         "Framework": "Flask", "Decision": "reject", "Rule_IDs_Checked": "MEM-002 considered; not confirmed"},
    ])
    return good, bad


# --- Stage 1: register import -----------------------------------------------------------------

def test_register_import_is_conservative(registers):
    good, bad = registers
    registry = import_registers(good, bad, {"url": "u", "commit": "c"})
    by_id = {c.candidate_id: c for c in registry.candidates}
    assert by_id["C001"].decision == "hold" and by_id["C001"].licence_verified
    assert by_id["C001"].register_commit is None  # "Not captured" is not a commit
    assert by_id["C001"].suspected_findings == []  # good candidates: checked rules are not defect claims
    assert by_id["C002"].decision == "needs_review" and not by_id["C002"].licence_verified
    assert by_id["C002"].register_commit == "0123456789abcdef0123456789abcdef01234567"
    claims = by_id["BAD-CAND-001"].suspected_findings
    assert [(c.rule_ids, c.file, c.line_start, c.line_end, c.ref) for c in claims] == [
        (["MEM-001"], "app/main.py", 10, 12, "main"), (["MEM-001"], "app/x.py", 5, 5, "main")]
    assert all(c.state == "unverified_claim" for c in claims)
    assert by_id["BAD-CAND-002"].suspected_findings == []  # "considered; not confirmed" is not a claim
    assert by_id["BAD-CAND-001"].app_id == "bad-cand-001" and by_id["C001"].app_id == "good-c001"
    assert "no pinned commit" in by_id["C001"].blockers()


def test_register_rejects_duplicate_ids(tmp_path):
    good = _write_csv(tmp_path / "g.csv", GOOD_COLUMNS, [{"Candidate_ID": "C1"}, {"Candidate_ID": "C1"}])
    with pytest.raises(ValueError, match="duplicate"):
        import_registers(good, None)


# --- Stage 2: fetch (offline) and screening -----------------------------------------------------

def test_fetch_pins_checks_out_and_drafts_manifest(registers, tmp_path, monkeypatch):
    registry = import_registers(*registers)
    calls = []

    def fake_git(args, cwd, hooks_dir):
        calls.append(args[0])
        if args[0] == "ls-remote":
            return "f" * 40 + "\tHEAD\n"
        if args[0] == "checkout":
            (cwd / "app").mkdir(exist_ok=True)
            (cwd / "app" / "main.py").write_text("def f(db, ids):\n    for i in ids:\n        db.get(U, i)\n", encoding="utf-8")
            (cwd / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        return ""

    monkeypatch.setattr(fetch_module, "_git", fake_git)
    apps = tmp_path / "apps"
    fetch_module.fetch_registry(registry, apps, log=lambda *_: None)
    fetched = [c for c in registry.candidates if c.fetch.status == "fetched"]
    assert {c.candidate_id for c in fetched} == {"C001", "C002", "BAD-CAND-001"}  # rejected one skipped
    assert next(c for c in registry.candidates if c.candidate_id == "BAD-CAND-002").fetch.status == "skipped"
    c002 = next(c for c in fetched if c.candidate_id == "C002")
    assert c002.fetch.commit.startswith("0123456789")  # register commit wins over ls-remote
    for candidate in fetched:
        app = apps / candidate.app_id
        assert not (app / ".git").exists()
        manifest = load_manifest(app)
        assert manifest.defects == [] and manifest.origin.source == "public" and manifest.origin.commit == candidate.fetch.commit
    # A team-edited manifest is never overwritten.
    target = apps / "good-c001" / MANIFEST_FILE
    target.write_text(target.read_text().replace("Draft written", "Edited by team"))
    fetch_module.fetch_registry(registry, apps, only={"C001"}, log=lambda *_: None)
    assert "Edited by team" in target.read_text()


def test_force_remove_refuses_paths_outside_root(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(fetch_module.FetchError):
        fetch_module._force_remove(outside, tmp_path / "apps")


def test_screening_locates_claims(registers, tmp_path):
    registry = import_registers(*registers)
    app = tmp_path / "bad-cand-001"
    (app / "app").mkdir(parents=True)
    (app / "app" / "main.py").write_text("\n" * 8 + "def load(db):\n    return db.query(User).all()\n    # end\n", encoding="utf-8")
    claims = next(c for c in registry.candidates if c.candidate_id == "BAD-CAND-001").suspected_findings
    result = screen_candidate(app, claims)
    first, second = result["claims"]
    assert first["status"] == "located" and first["function"] == "load" and first["scaletrace_rules_on_lines"] == ["MEM-001"]
    assert second["status"] == "file not present at pinned commit"


# --- Stage 4: final evaluation --------------------------------------------------------------------

def _mock_benchmark(root: Path) -> Path:
    for name, family in (("orders_app_defective", "orders"), ("orders_app_fixed", "orders"), ("shop_app", "shop")):
        target = root / name
        shutil.copytree(EXAMPLES / name, target, ignore=shutil.ignore_patterns("*.db", ".scaletrace", "__pycache__"))
        manifest = json.loads((target / MANIFEST_FILE).read_text())
        manifest["origin"]["source"] = "team"
        (target / MANIFEST_FILE).write_text(json.dumps(manifest))
    return root


def test_final_evaluation_requires_lock_and_logs(tmp_path):
    root = _mock_benchmark(tmp_path / "bench")
    rulebook = load_rulebook()
    from scaletrace.benchmark.cli import _load_all

    save_index(root, build_index(root, _load_all(root), 0.5, 0, ["template_family", "derived_from"]))
    with pytest.raises(FinalEvaluationError, match="not locked"):
        run_final(root, rulebook, [], tmp_path / "out")
    lock_index(root, load_index(root))
    splits = {e.app_id: e.split for e in load_index(root).apps}
    assert set(splits.values()) == {"dev", "test"}

    dry = run_final(root, rulebook, [], tmp_path / "dry", dry_run=True, log=lambda *_: None)
    assert dry["kind"] == "dry_run_on_dev" and not (root / "final_runs.log").exists()
    assert set(dry["apps"]) == {a for a, s in splits.items() if s == "dev"}

    final = run_final(root, rulebook, [], tmp_path / "final", log=lambda *_: None)
    assert final["kind"] == "final_on_test" and (root / "final_runs.log").read_text().count("\n") == 1
    for tier in ("source", "runtime"):
        overall = next(r for r in final["detection"][tier]["rows"] if r["level"] == "overall")
        assert overall["fp"] == 0 and overall["fn"] == 0 and overall["tp"] > 0
    assert (tmp_path / "final" / "final_report.md").read_text().startswith("# ScaleTrace benchmark evaluation: FINAL")


def test_final_evaluation_refuses_invalid_benchmark(tmp_path):
    root = _mock_benchmark(tmp_path / "bench")
    from scaletrace.benchmark.cli import _load_all

    save_index(root, build_index(root, _load_all(root), 0.5, 0, ["template_family", "derived_from"]))
    lock_index(root, load_index(root))
    test_app = next(e for e in load_index(root).apps if e.split == "test")
    (root / test_app.path / "app" / "main.py").write_text("# tampered\n")
    with pytest.raises(FinalEvaluationError, match="lock_violation"):
        run_final(root, load_rulebook(), [], tmp_path / "out")


def test_training_never_sees_benchmark_apps(tmp_path):
    from scaletrace.ml.experiment import load_bundles

    root = _mock_benchmark(tmp_path / "bench")
    with pytest.raises(TestSetAccessError):
        load_bundles([str(root)], "training", log=lambda *_: None)


# --- generator v5 ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shared_app(tmp_path_factory):
    for seed in range(40):
        root = tmp_path_factory.mktemp(f"v5_{seed}")
        labels = AppGenerator(seed=seed).generate(root / "app", n_defects=4, n_clean=0, kinds=SHARED_KINDS)
        shared = [s for s in labels["symptoms"] if s["mode"] == "shared_helper"]
        if shared:
            return root / "app", shared[0]
    raise AssertionError("no shared-helper symptom generated")


def test_v5_shared_helper_twin_is_statically_symmetric(shared_app):
    app, symptom = shared_app
    bundle = load_bundle(app)
    project = bundle.static.project
    root = project.functions[symptom["root_cause_functions"][0]]
    twin = project.functions[symptom["twin_decoy"]]
    helper = symptom["shared_helper"]
    assert helper in [c.callee for c in root.facts.calls] and helper in [c.callee for c in twin.facts.calls]
    from scaletrace.analyzer.project import unbounded

    # The role-defining part is identical: the shared helper is called inside an (apparently) unbounded
    # loop in both. Benign fragments are added independently at random, so per-function counts differ
    # by design; only their *distribution* is role-independent (checked by the fingerprint test).
    for info in (root, twin):
        assert any(c.callee == helper and unbounded(c.loops) for c in info.facts.calls)
    graph = bundle.static_graph
    assert set(graph.nodes[f"function:{root.func_id}"]["rules"]) >= {"MEM-001"}
    assert set(graph.nodes[f"function:{twin.func_id}"]["rules"]) >= {"MEM-001"}


def _edge_runtime(endpoint: str, root: str, twin: str, helper: str, twin_calls: list[float]) -> RuntimeReport:
    measurements = []
    for size, root_calls, tc in zip([10, 40, 160], [7, 37, 157], twin_calls):
        measurements.append(Measurement(
            size=size, requests=2, errors=0, queries_per_request=root_calls + tc + 2, latency_ms_mean=1.0,
            function_calls_per_request={root: 1.0, twin: 1.0, helper: root_calls + tc},
            queries_by_function_per_request={helper: root_calls + tc},
            call_edges=[ObservedEdge(caller=root, callee=helper, calls_per_request=root_calls),
                        ObservedEdge(caller=twin, callee=helper, calls_per_request=tc)],
        ))
    return RuntimeReport(scaletrace_version="t", repository="x", app="app.main:app", generated_at="now", status="completed",
                         instrumentation={"sqlalchemy_queries": True, "call_tracing": True},
                         endpoints=[EndpointRuntime(endpoint=endpoint, request_name="r", measurements=measurements)])


@pytest.mark.parametrize("twin_calls,expected", [([3, 3, 3], True), ([3, 30, 120], False)])
def test_verify_marks_graph_evidence(shared_app, tmp_path, twin_calls, expected):
    app, symptom = shared_app
    copy = tmp_path / "app"
    shutil.copytree(app, copy)
    (copy / ".scaletrace").mkdir(exist_ok=True)
    runtime = _edge_runtime(symptom["endpoint"], symptom["root_cause_functions"][0], symptom["twin_decoy"], symptom["shared_helper"], twin_calls)
    (copy / ".scaletrace" / "runtime.json").write_text(runtime.model_dump_json())
    verify_app(copy)
    stored = next(s for s in json.loads((copy / ROOTCAUSE_FILE).read_text())["symptoms"] if s["endpoint"] == symptom["endpoint"])
    assert stored["graph_required"] is expected
    assert stored["verification"]["graph_evidence"]["status"] == ("verified" if expected else "failed")


# --- models -----------------------------------------------------------------------------------

def _graph(n=4, e=3):
    x = torch.randn(n, 5)
    edge_index = torch.tensor([[0, 1, 2][:e], [1, 2, 3][:e]])
    return x, edge_index, torch.ones(e), torch.randn(e, EDGE_DIM)


def test_edge_conditioned_gnn_uses_edge_features():
    torch.manual_seed(0)
    model = RootCauseGNN(in_dim=5, hidden=8, layers=2, edge_dim=EDGE_DIM).eval()
    x, edge_index, weight, edge_attr = _graph()
    assert not torch.allclose(model(x, edge_index, weight, edge_attr), model(x, edge_index, weight, torch.zeros_like(edge_attr)))
    legacy = RootCauseGNN(in_dim=5, hidden=8, layers=2).eval()
    assert torch.allclose(legacy(x, edge_index, weight, edge_attr), legacy(x, edge_index, weight))  # edge_dim 0 ignores them


def test_neighbor_mlp_and_collate_with_edges():
    model = NeighborMLP(in_dim=5, hidden=8, layers=2).eval()
    graphs = []
    for n in (4, 3):
        x, edge_index, weight, edge_attr = _graph(n, n - 1)
        graphs.append((x, edge_index, weight, edge_attr, torch.arange(n), torch.zeros(n)))
    x, edge_index, weight, edge_attr, candidates, _ = collate(graphs)
    joint = model(x, edge_index, weight, edge_attr)
    separate = torch.cat([model(g[0], g[1], g[2], g[3]) for g in graphs])
    assert torch.allclose(joint, separate, atol=1e-6)
    assert [c.tolist() for c in candidates] == [[0, 1, 2, 3], [4, 5, 6]]


def test_mlp_hop_checkpoint_round_trip_and_legacy_checkpoint(shared_app, tmp_path):
    from scaletrace.ml.dataset import build_samples
    from scaletrace.ml.embeddings import HashedCodeEmbedder
    from scaletrace.ml.features import VARIANT_GROUPS, FeatureBuilder

    app, _ = shared_app
    builder = FeatureBuilder(VARIANT_GROUPS["static"], HashedCodeEmbedder(8), 8)
    samples = build_samples(load_bundle(app), builder, use_runtime=False)
    for architecture, edges in (("mlp_hop", True), ("gnn", False)):
        result = train_model(samples, [], builder.names, TrainConfig(epochs=2, hidden=8, layers=2, architecture=architecture, edge_features=edges),
                             log=lambda *_: None)
        path = save_checkpoint(tmp_path / f"{architecture}.pt", result, {"variant": "static", "groups": builder.groups, "embedder": "hashed",
                                                                        "code_dim": 8, "feature_names": builder.names})
        model, checkpoint = load_checkpoint(path)
        assert checkpoint["architecture"] == architecture
        assert checkpoint["model_config"]["edge_dim"] == (EDGE_DIM if edges else 0)


def test_edge_aware_heuristic_is_off_by_default():
    from scaletrace.diagnosis import DiagnosisConfig

    assert DiagnosisConfig().edge_growth is False


def test_final_evaluation_reports_controlled_copies_separately(tmp_path):
    root = _mock_benchmark(tmp_path / "bench")
    target = root / "orders_app_defective" / MANIFEST_FILE
    manifest = json.loads(target.read_text())
    manifest["origin"].update({"controlled": True, "derived_from": "orders_app_fixed"})
    target.write_text(json.dumps(manifest))
    from scaletrace.benchmark.cli import _load_all

    save_index(root, build_index(root, _load_all(root), 0.5, 0, ["template_family", "derived_from"]))
    lock_index(root, load_index(root))
    split = next(e.split for e in load_index(root).apps if e.app_id == "orders_app_defective")
    results = run_final(root, load_rulebook(), [], tmp_path / "out", dry_run=split == "dev", log=lambda *_: None)
    assert results["controlled_apps"] == ["orders_app_defective"]
    for tier in ("source", "runtime"):
        assert "orders_app_defective" in results["detection_controlled"][tier]["per_app"]
        assert "orders_app_defective" not in results["detection"][tier]["per_app"]
    assert "CONTROLLED COPIES ONLY" in (tmp_path / "out" / "final_report.md").read_text()
