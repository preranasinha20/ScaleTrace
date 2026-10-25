"""Labelling workflow: route registration fix, two verification tiers, review sheets and apply-review."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from scaletrace.benchmark import MANIFEST_FILE, load_manifest
from scaletrace.benchmark.fetch import DRAFT_MARKER, draft_manifest, is_untouched_draft
from scaletrace.benchmark.loader import groundtruth_from_manifest, positive_defects
from scaletrace.benchmark.register import Candidate, FetchInfo, SuspectedFinding
from scaletrace.benchmark.review import REVIEW_COPY, ReviewError, apply_review, load_review, write_review
from scaletrace.benchmark.schema import AppManifest, Defect, Origin, Verification
from scaletrace.pipeline import run_static_analysis
from scaletrace.rules import load_rulebook

RULEBOOK = load_rulebook()


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return root


# --- route registration ------------------------------------------------------------------------

def test_add_api_route_and_include_router_prefixes(tmp_path):
    _write(tmp_path, {
        "backend/__init__.py": "",
        "backend/controllers/__init__.py": "",
        "backend/controllers/users.py": """
            def get_all_users():
                return []

            def create_user():
                return {}
        """,
        "backend/routes/__init__.py": "",
        "backend/routes/user_routes.py": """
            from fastapi import APIRouter
            from controllers.users import create_user, get_all_users

            USER_ROUTER = APIRouter(prefix="/user")
            USER_ROUTER.add_api_route("/all", get_all_users, methods=["GET"])
            USER_ROUTER.add_api_route("/signup", endpoint=create_user, methods=["POST"])
        """,
        "backend/routes/annotated.py": """
            import typing
            import fastapi

            ROUTER: typing.Final = fastapi.APIRouter()

            @ROUTER.get("/decks/")
            def decks():
                return []
        """,
        "backend/main.py": """
            from fastapi import FastAPI
            from routes.user_routes import USER_ROUTER
            from routes import annotated

            server = FastAPI()
            server.include_router(USER_ROUTER, prefix="/api")

            def include(app):
                app.include_router(annotated.ROUTER, prefix="/v2")
        """,
    })
    endpoints = run_static_analysis(tmp_path).project.endpoints()
    assert endpoints["GET /api/user/all"].qualname == "get_all_users"
    assert endpoints["POST /api/user/signup"].qualname == "create_user"
    assert endpoints["GET /v2/decks/"].qualname == "decks"


def test_existing_decorator_routes_unchanged(tmp_path):
    _write(tmp_path, {"app.py": """
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        router = APIRouter(prefix="/x")

        @router.get("/a")
        def a():
            return 1

        @app.post("/b")
        def b():
            return 2

        app.include_router(router)
    """})
    assert sorted(run_static_analysis(tmp_path).project.endpoints()) == ["GET /x/a", "POST /b"]


# --- two tiers -----------------------------------------------------------------------------------

def _defect(defect_id: str, static: str, runtime: str) -> Defect:
    return Defect(defect_id=defect_id, rule_id="MEM-001", category="memory_resource_exhaustion", file="a.py",
                  function="f", line_start=1, line_end=2, verification=Verification(static=static, runtime=runtime))


def test_source_and_runtime_tiers_are_distinct():
    manifest = AppManifest(app_id="x", kind="bad", origin=Origin(source="team"), defects=[
        _defect("x-1", "verified", "unverified"), _defect("x-2", "verified", "verified"),
        _defect("x-3", "unverified", "unverified"), _defect("x-4", "verified", "rejected"),
    ])
    assert [d.defect_id for d in positive_defects(manifest, "source")] == ["x-1", "x-2", "x-4"]
    assert [d.defect_id for d in positive_defects(manifest, "runtime")] == ["x-2"]
    assert [e.note for e in groundtruth_from_manifest(manifest, ["MEM-001"], "source").expected_findings] == ["x-1", "x-2", "x-4"]
    with pytest.raises(ValueError):
        positive_defects(manifest, "both")


# --- drafts, sheets, apply-review ----------------------------------------------------------------

APP_CODE = {
    "app/__init__.py": "",
    "app/main.py": """
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        router = APIRouter(prefix="/user")

        def get_all_users(db):
            users = db.query(User).all()
            return [u.id for u in users]

        router.add_api_route("/all", get_all_users, methods=["GET"])
        app.include_router(router)
    """,
}


@pytest.fixture
def candidate_app(tmp_path):
    apps = tmp_path / "apps"
    candidate = Candidate(
        candidate_id="BAD-CAND-099", app_id="bad-cand-099", intended_kind="bad", repository="o/r", url="https://github.com/o/r",
        decision="retain_candidate", decision_raw="retain_candidate", framework="FastAPI", is_fastapi=True,
        licence="Not verified", licence_status="", licence_verified=False,
        suspected_findings=[SuspectedFinding(rule_ids=["MEM-001"], file="app/main.py", line_start=6, line_end=8, summary="loads all users")],
        fetch=FetchInfo(status="fetched", commit="a" * 40, pinned_at="2026-10-25T00:00:00+00:00"),
    )
    _write(apps / candidate.app_id, APP_CODE)
    assert draft_manifest(candidate, apps) is not None
    return candidate, apps


def test_draft_records_unknown_licence_and_commit(candidate_app):
    candidate, apps = candidate_app
    manifest = load_manifest(apps / candidate.app_id)
    assert manifest.origin.license is None and "Licence: UNKNOWN" in manifest.description
    assert "screening team inspected is UNKNOWN" in manifest.description and manifest.origin.commit == "a" * 40
    assert is_untouched_draft(manifest) and DRAFT_MARKER in manifest.description


def test_review_sheet_merges_claim_and_hit_and_starts_undecided(candidate_app, tmp_path):
    candidate, apps = candidate_app
    md, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    review = load_review(js)
    [item] = review.items
    assert set(item.sources) == {"screening_claim", "scaletrace_hit"}
    assert item.function == "get_all_users" and item.endpoint_candidates == ["GET /user/all"]
    assert item.decision == "undecided" and review.app.decision == "undecided" and review.app.licence == "unknown"
    assert "db.query(User).all()" in md.read_text()
    assert write_review(candidate, apps / candidate.app_id, tmp_path / "review") is None  # never overwrites decisions


def _fill(path: Path, **changes) -> Path:
    data = json.loads(path.read_text())
    for key, value in changes.items():
        target = data
        *parents, leaf = key.split(".")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[leaf] = value
    path.write_text(json.dumps(data))
    return path


def test_apply_review_writes_only_explicit_decisions(candidate_app, tmp_path):
    candidate, apps = candidate_app
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    # Nothing decided -> nothing written except review provenance; licence stays unknown.
    summary = apply_review(js, apps, RULEBOOK)
    manifest = load_manifest(apps / candidate.app_id)
    assert summary["defects"] == 0 and len(summary["open_items"]) == 1 and manifest.defects == []
    assert manifest.origin.license is None and not is_untouched_draft(manifest)

    _fill(js, **{"reviewer": "Asha", "review_date": "2026-10-26", "app.decision": "keep",
                 "items.0.decision": "defect", "items.0.reason": "Loads every user; no limit or pagination on GET /user/all.",
                 "items.0.endpoint": "GET /user/all", "items.0.symptom_kind": "memory_growth",
                 "items.0.root_cause_function": "app.main:get_all_users", "items.0.static_verified_by": "Ravi"})
    summary = apply_review(js, apps, RULEBOOK)
    manifest = load_manifest(apps / candidate.app_id)
    [defect] = manifest.defects
    assert summary["source_confirmed"] == 1
    assert defect.verification.static == "verified" and defect.verification.runtime == "unverified"
    assert defect.verification.verified_by == "Ravi" and "not executed" in defect.verification.notes
    assert (apps / candidate.app_id / REVIEW_COPY).exists()
    apply_review(js, apps, RULEBOOK)  # idempotent
    assert len(load_manifest(apps / candidate.app_id).defects) == 1


def test_apply_review_unconfirmed_defect_is_recorded_but_not_source_confirmed(candidate_app, tmp_path):
    candidate, apps = candidate_app
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    _fill(js, **{"items.0.decision": "defect", "items.0.reason": "looks unbounded"})
    apply_review(js, apps, RULEBOOK)
    [defect] = load_manifest(apps / candidate.app_id).defects
    assert defect.verification.static == "unverified"
    assert positive_defects(load_manifest(apps / candidate.app_id), "source") == []


@pytest.mark.parametrize("changes,message", [
    ({"items.0.decision": "defect"}, "needs a reason"),
    ({"items.0.decision": "not_a_defect"}, "needs a reason"),
    ({"app.decision": "exclude"}, "exclusion_reason"),
    ({"app.licence": "MIT"}, "licence_evidence"),
    ({"items.0.decision": "defect", "items.0.reason": "r", "items.0.runtime": "not_applicable", "items.0.runtime_note": "n"}, "only allowed"),
])
def test_apply_review_refuses_unjustified_decisions(candidate_app, tmp_path, changes, message):
    candidate, apps = candidate_app
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    before = (apps / candidate.app_id / MANIFEST_FILE).read_text()
    with pytest.raises(ReviewError, match=message):
        apply_review(_fill(js, **changes), apps, RULEBOOK)
    assert (apps / candidate.app_id / MANIFEST_FILE).read_text() == before


def test_runtime_verified_cannot_be_claimed_in_a_review(candidate_app, tmp_path):
    candidate, apps = candidate_app
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    with pytest.raises(ReviewError, match="invalid review file"):
        apply_review(_fill(js, **{"items.0.runtime": "verified"}), apps, RULEBOOK)


def test_exclusion_and_not_a_defect(candidate_app, tmp_path):
    candidate, apps = candidate_app
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    _fill(js, **{"app.decision": "exclude", "app.exclusion_reason": "not a FastAPI application",
                 "items.0.decision": "not_a_defect", "items.0.reason": "admin-only table, capped at 50 rows"})
    apply_review(js, apps, RULEBOOK)
    manifest = load_manifest(apps / candidate.app_id)
    assert manifest.split == "excluded" and manifest.exclusion_reason == "not a FastAPI application"
    assert manifest.known_non_defects[0].reason.endswith("capped at 50 rows")


def test_claim_spanning_functions_is_attributed_to_the_hit(candidate_app, tmp_path):
    candidate, apps = candidate_app
    # A screening claim whose range starts above the function (module code) still lands on the flagged function.
    candidate.suspected_findings[0].line_start = 1
    _, js = write_review(candidate, apps / candidate.app_id, tmp_path / "review")
    [item] = load_review(js).items
    assert set(item.sources) == {"screening_claim", "scaletrace_hit"}
    assert item.function == "get_all_users" and item.line_start == 7


def test_parent_router_prefix_applies_to_included_routers(tmp_path):
    _write(tmp_path, {
        "app/__init__.py": "",
        "app/user.py": """
            from fastapi import APIRouter

            user_router = APIRouter(prefix="/user")

            @user_router.post("/bulk")
            def bulk():
                return 1
        """,
        "app/routes.py": """
            from fastapi import APIRouter
            from app.user import user_router

            router = APIRouter(prefix="/api")
            router.include_router(user_router)
        """,
        "main.py": """
            from fastapi import FastAPI
            from app.routes import router as api_router

            app = FastAPI()
            app.include_router(api_router)
        """,
    })
    assert "POST /api/user/bulk" in run_static_analysis(tmp_path).project.endpoints()
