"""Pinning and fetching candidate repositories for STATIC analysis only.

Safety: repositories are downloaded with git (hooks disabled, symlinks not created, LFS smudging and
credential prompts off), checked out at one pinned commit, and the ``.git`` directory is removed. No
file from a fetched repository is ever imported, installed or executed by ScaleTrace; static analysis
only parses source text.

Pinning: the commit is the repository's HEAD at fetch time (recorded with a timestamp). The team's
screening links point at a branch, so the screening report also checks whether each claimed location
still exists at the pinned commit.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scaletrace.benchmark.register import Candidate, Registry
from scaletrace.benchmark.schema import MANIFEST_FILE, AppManifest, Origin, RunInfo

GIT_TIMEOUT_S = 180
FETCHABLE = {"accepted", "retain_candidate", "modify_for_benchmark", "needs_review", "hold"}


class FetchError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GCM_INTERACTIVE": "never"})
    return env


def _git(args: list[str], cwd: Path | None, hooks_dir: Path) -> str:
    command = [
        "git", "-c", f"core.hooksPath={hooks_dir.as_posix()}", "-c", "core.symlinks=false",
        "-c", "protocol.file.allow=never", "-c", "submodule.recurse=false", "-c", "credential.helper=", *args,
    ]
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=_git_env())
    if done.returncode != 0:
        raise FetchError((done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout).strip() else f"git exited {done.returncode}")
    return done.stdout


def _force_remove(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if root.resolve() not in resolved.parents:
        raise FetchError(f"refusing to delete {resolved}: outside {root}")

    def on_error(func, target, _exc):  # git marks pack files read-only on Windows
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(resolved, onexc=on_error)
    else:  # pragma: no cover
        shutil.rmtree(resolved, onerror=on_error)


def pin(candidate: Candidate, hooks_dir: Path) -> str:
    output = _git(["ls-remote", "--exit-code", candidate.url, "HEAD"], None, hooks_dir)
    sha = output.split()[0] if output.split() else ""
    if len(sha) != 40:
        raise FetchError(f"unexpected ls-remote output: {output[:80]!r}")
    return sha


def fetch_candidate(candidate: Candidate, apps_dir: Path, force: bool = False) -> Candidate:
    """Pin and fetch one candidate into ``apps_dir/<app_id>``; updates and returns ``candidate``."""
    target = apps_dir / candidate.app_id
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory(dir=apps_dir) as empty_hooks:
        hooks_dir = Path(empty_hooks)
        try:
            sha = candidate.register_commit or candidate.fetch.commit or pin(candidate, hooks_dir)
        except (FetchError, subprocess.TimeoutExpired) as exc:
            candidate.fetch.status, candidate.fetch.error = "unavailable", f"pin failed: {exc}"
            return candidate
        candidate.fetch.commit, candidate.fetch.pinned_at = sha, candidate.fetch.pinned_at or now
        if target.exists() and not force and (target / ".scaletrace_fetch").exists():
            candidate.fetch.status, candidate.fetch.path = "fetched", target.name
            return candidate
        if target.exists():
            _force_remove(target, apps_dir)
        target.mkdir(parents=True)
        try:
            _git(["init", "-q"], target, hooks_dir)
            _git(["remote", "add", "origin", candidate.url], target, hooks_dir)
            _git(["fetch", "-q", "--depth", "1", "--no-tags", "origin", sha], target, hooks_dir)
            _git(["checkout", "-q", "FETCH_HEAD"], target, hooks_dir)
        except (FetchError, subprocess.TimeoutExpired) as exc:
            _force_remove(target, apps_dir)
            candidate.fetch.status, candidate.fetch.error = "failed", f"fetch failed: {exc}"
            return candidate
    _force_remove(target / ".git", apps_dir)
    (target / ".scaletrace_fetch").write_text(f"{candidate.url}\n{sha}\n{now}\n", encoding="utf-8")
    candidate.fetch.status, candidate.fetch.path, candidate.fetch.error = "fetched", target.name, None
    candidate.fetch.python_files = sum(1 for _ in target.rglob("*.py"))
    return candidate


DRAFT_MARKER = "Draft written by `scaletrace benchmark fetch`"


def is_untouched_draft(manifest: AppManifest) -> bool:
    """True if a manifest is exactly as drafted (safe to regenerate): marker present, nothing labelled."""
    return DRAFT_MARKER in manifest.description and not manifest.defects and not manifest.known_non_defects and manifest.split == "unassigned"


def draft_manifest(candidate: Candidate, apps_dir: Path, replace_untouched: bool = False) -> Path | None:
    """Write a manifest skeleton (no defects) unless one already exists. Never overwrites team work.

    Licence: recorded only if the screening register shows it as verified; otherwise ``None``
    (unknown), with the register's wording kept in the description. Commit: the commit ScaleTrace
    pinned and fetched; the commit the screening team inspected was not recorded and is stated as unknown.
    """
    from scaletrace.benchmark.loader import load_manifest

    target = apps_dir / candidate.app_id / MANIFEST_FILE
    if candidate.fetch.status != "fetched":
        return None
    if target.exists() and not (replace_untouched and is_untouched_draft(load_manifest(target.parent))):
        return None
    owner = candidate.repository.split("/", 1)[0] if "/" in candidate.repository else None
    licence_note = (f"Licence: {candidate.licence} (verified in screening)." if candidate.licence_verified
                    else f"Licence: UNKNOWN (screening register says {candidate.licence or 'nothing'!r}; not verified).")
    manifest = AppManifest(
        app_id=candidate.app_id,
        kind=candidate.intended_kind,
        description=(
            f"{candidate.repository} (screening decision: {candidate.decision}). {licence_note} "
            f"Commit: {candidate.fetch.commit} pinned by ScaleTrace on {(candidate.fetch.pinned_at or '')[:10]}; "
            f"the commit the screening team inspected is UNKNOWN. {DRAFT_MARKER}; defects must be added and "
            "verified by a person."
        ),
        origin=Origin(source="public", author=owner, repo_url=candidate.url, commit=candidate.fetch.commit,
                      license=candidate.licence if candidate.licence_verified else None),
        run=RunInfo(runtime_status="not_attempted",
                    notes="Untrusted public repository: runtime testing requires the Phase 2 sandbox."),
    )
    target.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return target


def fetch_registry(registry: Registry, apps_dir: Path, only: set[str] | None = None, include_rejected: bool = False,
                   force: bool = False, log=print) -> Registry:
    apps_dir.mkdir(parents=True, exist_ok=True)
    for candidate in registry.candidates:
        if only and candidate.candidate_id not in only:
            continue
        if candidate.decision not in FETCHABLE and not include_rejected:
            candidate.fetch.status = "skipped" if candidate.fetch.status == "not_fetched" else candidate.fetch.status
            continue
        fetch_candidate(candidate, apps_dir, force)
        drafted = draft_manifest(candidate, apps_dir)
        log(f"  {candidate.candidate_id:<14} {candidate.repository:<50} {candidate.fetch.status:<11} "
            f"{(candidate.fetch.commit or '')[:12]} {candidate.fetch.python_files or '':>5} "
            f"{'manifest drafted' if drafted else ''}{candidate.fetch.error or ''}")
    return registry
