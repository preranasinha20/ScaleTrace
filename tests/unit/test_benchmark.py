"""Workstream A acceptance tests: manifest schema, validator error codes, split, lock, guard, evaluation."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scaletrace.benchmark import (
    MANIFEST_FILE,
    ManifestError,
    TestSetAccessError,
    check_access,
    groundtruth_from_manifest,
    load_manifest,
    ml_labels_from_manifest,
    record_final_run,
    validate_benchmark,
)
from scaletrace.benchmark.loader import load_index, save_index
from scaletrace.benchmark.split import SplitError, assign_splits, build_index, lock_index
from scaletrace.rules import load_rulebook
from tests.conftest import EXAMPLES, REPO_ROOT

RULEBOOK = load_rulebook()
APP_SOURCE = textwrap.dedent(
    """
    from fastapi import Depends, FastAPI
    from sqlalchemy.orm import Session

    app = FastAPI()


    @app.get("/orders")
    def list_orders(db: Session = Depends(get_db)):
        users = db.query(User).limit(10).all()
        out = []
        for user in users:
            out.append(db.query(Order).filter(Order.user_id == user.id).count())
        return out
    """
).lstrip()
DEFECT_LINE = 12  # the db.query inside the loop


def verified(runtime: str = "verified") -> dict:
    return {"static": "verified", "runtime": runtime, "verified_by": "tester", "date": "2026-10-25"}


def make_defect(defect_id: str, **overrides) -> dict:
    defect = {
        "defect_id": defect_id, "rule_id": "DB-001", "category": "repeated_database_queries",
        "file": "app/main.py", "function": "list_orders", "line_start": DEFECT_LINE, "line_end": DEFECT_LINE,
        "endpoint": "GET /orders", "symptom_kind": "query_growth",
        "root_cause_functions": ["app.main:list_orders"],
        "path": ["endpoint:GET /orders", "function:app.main:list_orders", "resource:database"],
        "description": "query per user", "verification": verified(),
    }
    defect.update(overrides)
    return defect


def make_app(root: Path, app_id: str, kind: str = "bad", defects: list[dict] | None = None, source: str = "team",
             family: str | None = None, derived_from: str | None = None, split: str = "unassigned", **extra) -> Path:
    app_dir = root / app_id
    (app_dir / "app").mkdir(parents=True, exist_ok=True)
    (app_dir / "app" / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "app" / "main.py").write_text(APP_SOURCE, encoding="utf-8")
    if defects is None:
        defects = [make_defect(f"{app_id}-d1")] if kind == "bad" else []
    manifest = {
        "app_id": app_id, "kind": kind, "origin": {"source": source, "template_family": family, "derived_from": derived_from},
        "split": split, "defects": defects, **extra,
    }
    (app_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return app_dir


def codes(root: Path, level: str = "error") -> set[str]:
    return validate_benchmark(root, RULEBOOK, check_duplicates=False).codes(level)


# --- schema -------------------------------------------------------------------------------------

def test_schema_rejects_bad_line_order_and_unjustified_exclusion(tmp_path):
    make_app(tmp_path, "a1", defects=[make_defect("a1-d1", line_start=20, line_end=10)])
    with pytest.raises(ManifestError, match="line_end"):
        load_manifest(tmp_path / "a1")
    make_app(tmp_path, "a2", split="excluded")
    with pytest.raises(ManifestError, match="exclusion_reason"):
        load_manifest(tmp_path / "a2")
    assert "schema_invalid" in codes(tmp_path)


def test_clean_benchmark_is_valid(tmp_path):
    make_app(tmp_path, "bad-1")
    make_app(tmp_path, "good-1", kind="good", known_non_defects=[
        {"rule_id": "DB-001", "file": "app/main.py", "line": DEFECT_LINE, "reason": "reference copy; reviewed"},
    ])
    report = validate_benchmark(tmp_path, RULEBOOK)
    assert report.errors == []
    assert report.warnings == []
    assert [a.verified for a in sorted(report.apps, key=lambda a: a.app_id)] == [1, 0]


# --- validator error codes (each seeded error must be caught) ------------------------------------

@pytest.mark.parametrize("override,code", [
    ({"file": "app/missing.py"}, "file_missing"),
    ({"function": "nope"}, "function_missing"),
    ({"line_start": 1, "line_end": 2}, "line_outside_function"),
    ({"rule_id": "XYZ-999"}, "unknown_rule"),
    ({"category": "inefficient_computation"}, "category_mismatch"),
    ({"endpoint": "GET /missing"}, "endpoint_missing"),
    ({"root_cause_functions": ["app.main:ghost"]}, "root_cause_missing"),
])
def test_defect_location_errors(tmp_path, override, code):
    make_app(tmp_path, "bad-1", defects=[make_defect("bad-1-d1", **override)])
    assert code in codes(tmp_path)


def test_app_level_errors(tmp_path):
    make_app(tmp_path, "dup", defects=[make_defect("x-d1"), make_defect("x-d1")])
    make_app(tmp_path, "empty-bad", defects=[])
    make_app(tmp_path, "good-unjustified", kind="good", defects=[make_defect("g-d1", description="")])
    make_app(tmp_path, "orphan", derived_from="does-not-exist")
    found = codes(tmp_path)
    assert {"duplicate_defect_id", "bad_without_defects", "good_defect_unjustified", "derived_from_unknown"} <= found


def test_duplicate_app_id(tmp_path):
    make_app(tmp_path, "one")
    clone = make_app(tmp_path, "two")
    data = json.loads((clone / MANIFEST_FILE).read_text())
    data["app_id"] = "one"
    data["defects"][0]["defect_id"] = "other-d1"
    (clone / MANIFEST_FILE).write_text(json.dumps(data))
    assert "duplicate_app_id" in codes(tmp_path)


def test_warnings_for_unverified_missed_and_unreviewed(tmp_path):
    make_app(tmp_path, "source-only", defects=[make_defect("p-d1", verification={"static": "verified", "runtime": "unverified"})])
    make_app(tmp_path, "pending", defects=[make_defect("p-d2", verification={"static": "unverified", "runtime": "unverified"})])
    make_app(tmp_path, "missed", defects=[make_defect("m-d1", rule_id="CMP-001", category="inefficient_computation", symptom_kind="latency_growth")])
    make_app(tmp_path, "good-hit", kind="good")  # the DB-001 pattern exists but is not documented
    warnings = codes(tmp_path, "warning")
    assert {"defect_source_only", "defect_unverified", "rule_misses_defect", "unreviewed_rule_hit"} <= warnings


def test_known_non_defect_silences_review_warning(tmp_path):
    make_app(tmp_path, "good-ok", kind="good", known_non_defects=[
        {"rule_id": "DB-001", "file": "app/main.py", "line": DEFECT_LINE, "reason": "users are capped at 10 by the limit"},
    ])
    assert "unreviewed_rule_hit" not in codes(tmp_path, "warning")


# --- split, lock -------------------------------------------------------------------------------

def _apps(root: Path) -> dict:
    from scaletrace.benchmark.cli import _load_all

    return _load_all(root)


def test_split_is_deterministic_grouped_and_stratified(tmp_path):
    for i in range(10):
        make_app(tmp_path, f"good-{i}", kind="good", family=f"fam-{i}")
        make_app(tmp_path, f"bad-{i}", derived_from=f"good-{i}", family=f"fam-{i}", defects=[make_defect(f"bad-{i}-d1")])
    apps = _apps(tmp_path)
    first = build_index(tmp_path, apps, 0.2, seed=7, group_by=["template_family", "derived_from"])
    second = build_index(tmp_path, apps, 0.2, seed=7, group_by=["template_family", "derived_from"])
    assert first.model_dump() == second.model_dump()
    split = {e.app_id: e.split for e in first.apps}
    for i in range(10):
        assert split[f"good-{i}"] == split[f"bad-{i}"]  # derived pairs stay together
    assert sum(1 for e in first.apps if e.split == "dev" and e.kind == "good") == 2
    assert sum(1 for e in first.apps if e.split == "dev" and e.kind == "bad") == 2


def test_excluded_apps_stay_excluded(tmp_path):
    make_app(tmp_path, "x", split="excluded", exclusion_reason="cannot be parsed")
    make_app(tmp_path, "y")
    manifests = {a: m for a, (_, m) in _apps(tmp_path).items()}
    assert assign_splits(manifests, 0.5, 0, ["template_family"])["x"] == "excluded"


def test_split_group_violation_is_reported(tmp_path):
    make_app(tmp_path, "g", kind="good", family="f", split="dev")
    make_app(tmp_path, "b", family="f", split="test")
    assert "split_group_violation" in codes(tmp_path)


def test_lock_detects_changes_and_blocks_resplit(tmp_path):
    for i in range(5):
        make_app(tmp_path, f"bad-{i}", family=f"f{i}", defects=[make_defect(f"bad-{i}-d1")])
    index = build_index(tmp_path, _apps(tmp_path), 0.2, 0, ["template_family"])
    save_index(tmp_path, index)
    assert "not_in_index" not in codes(tmp_path)
    lock_index(tmp_path, load_index(tmp_path))
    assert codes(tmp_path) == set()
    with pytest.raises(SplitError, match="locked"):
        build_index(tmp_path, _apps(tmp_path), 0.2, 0, ["template_family"])

    test_app = next(e for e in load_index(tmp_path).apps if e.split == "test")
    main = tmp_path / test_app.path / "app" / "main.py"
    main.write_text(main.read_text() + "\n# edited\n")
    assert "lock_violation" in codes(tmp_path)
    make_app(tmp_path, "late-addition", family="new")
    assert "not_in_index" in codes(tmp_path)


# --- guard -------------------------------------------------------------------------------------

def test_guard_levels(tmp_path):
    for i in range(5):
        make_app(tmp_path, f"bad-{i}", family=f"f{i}", defects=[make_defect(f"bad-{i}-d1")])
    save_index(tmp_path, build_index(tmp_path, _apps(tmp_path), 0.2, 0, ["template_family"]))
    entries = load_index(tmp_path).apps
    dev = tmp_path / next(e.path for e in entries if e.split == "dev")
    test = tmp_path / next(e.path for e in entries if e.split == "test")

    for app in (dev, test):
        with pytest.raises(TestSetAccessError, match="never be used for training"):
            check_access([app], "training")
    check_access([dev], "diagnostic")
    with pytest.raises(TestSetAccessError, match="test split"):
        check_access([test], "diagnostic")
    with pytest.raises(TestSetAccessError, match="locked"):
        check_access([test], "final")
    lock_index(tmp_path, load_index(tmp_path))
    check_access([test], "final")

    model = tmp_path / "m.pt"
    model.write_bytes(b"weights")
    log = record_final_run(tmp_path, [model], ["bad-0"], "unit test")
    record_final_run(tmp_path, [model], ["bad-0"], "unit test")
    assert len(log.read_text().splitlines()) == 2


def test_unindexed_team_app_is_still_protected_from_training(tmp_path):
    app = make_app(tmp_path, "solo")
    with pytest.raises(TestSetAccessError):
        check_access([app], "training")
    generated = make_app(tmp_path, "gen", source="generated")
    check_access([generated], "training")


def test_ml_loader_applies_guard(tmp_path):
    from scaletrace.ml.dataset import find_apps

    make_app(tmp_path, "team-app")
    with pytest.raises(TestSetAccessError):
        find_apps([tmp_path], access="training")
    assert len(find_apps([tmp_path], access="diagnostic")) == 1


# --- label conversion and evaluation ---------------------------------------------------------------

def test_only_verified_defects_become_labels(tmp_path):
    make_app(tmp_path, "mix", defects=[
        make_defect("mix-d1"),
        make_defect("mix-d2", verification={"static": "verified", "runtime": "unverified"}),
        make_defect("mix-d3", verification={"static": "rejected", "runtime": "unverified"}),
        make_defect("mix-d4", rule_id="EXT-001", category="external_service_calls", symptom_kind=None,
                    verification=verified("not_applicable")),
    ])
    manifest = load_manifest(tmp_path / "mix")
    truth = groundtruth_from_manifest(manifest, ["DB-001", "EXT-001"])
    assert [e.note for e in truth.expected_findings] == ["mix-d1", "mix-d4"]
    labels = ml_labels_from_manifest(manifest)
    assert [s["defect_id"] for s in labels["symptoms"]] == ["mix-d1"]  # d4 has no symptom kind


def test_examples_have_identical_metrics_with_manifests(tmp_path):
    apps = [str(EXAMPLES / a) for a in ("orders_app_defective", "orders_app_fixed", "shop_app")]

    def overall(*extra: str) -> str:
        done = subprocess.run([sys.executable, "-m", "scaletrace", "evaluate", *apps, *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        return next(line for line in done.stdout.splitlines() if line.startswith("overall"))

    assert overall() == overall("--manifest")
    assert overall().split()[2:5] == ["10", "0", "0"]


def test_validate_cli_exit_codes(tmp_path):
    make_app(tmp_path, "ok")
    run = lambda: subprocess.run([sys.executable, "-m", "scaletrace", "benchmark", "validate", str(tmp_path)],
                                 cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert run().returncode == 0
    make_app(tmp_path, "broken", defects=[make_defect("broken-d1", function="missing")])
    done = run()
    assert done.returncode == 1 and "function_missing" in done.stdout


def test_controlled_copy_needs_base_and_follows_its_exclusion(tmp_path):
    app = make_app(tmp_path, "copy")
    manifest = json.loads((app / MANIFEST_FILE).read_text())
    manifest["origin"]["controlled"] = True
    (app / MANIFEST_FILE).write_text(json.dumps(manifest))
    assert "controlled_without_base" in codes(tmp_path)

    make_app(tmp_path, "base", kind="good", split="excluded", exclusion_reason="rejected in review")
    manifest["origin"]["derived_from"] = "base"
    (app / MANIFEST_FILE).write_text(json.dumps(manifest))
    assert "controlled_base_excluded" in codes(tmp_path) and "controlled_without_base" not in codes(tmp_path)


def test_excluded_bad_app_needs_no_defects(tmp_path):
    make_app(tmp_path, "b1", defects=[])
    assert "bad_without_defects" in codes(tmp_path)
    make_app(tmp_path, "b1", defects=[], split="excluded", exclusion_reason="not a FastAPI application")
    assert "bad_without_defects" not in codes(tmp_path)
