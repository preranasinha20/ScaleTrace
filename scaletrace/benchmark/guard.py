"""Keeping the benchmark out of training and model selection.

Three access levels:

* ``training``  -- training, early stopping, hyperparameters, variant choice. Refuses EVERY benchmark
  application (team or public origin, or listed in a benchmark index), dev and test alike.
* ``diagnostic`` -- non-final evaluation (e.g. realism checks on dev). Refuses test applications.
* ``final``      -- the one-off final evaluation. Requires a locked index; each run is appended to
  ``final_runs.log`` next to the index so the number of test-set accesses is auditable.

The guard raises; it never silently skips an application, so a misconfigured command fails loudly.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from scaletrace.benchmark.loader import find_index_for, has_manifest, load_manifest
from scaletrace.benchmark.schema import FINAL_RUNS_LOG

Access = Literal["training", "diagnostic", "final"]


class TestSetAccessError(PermissionError):
    """An operation tried to use benchmark data it is not allowed to see."""

    __test__ = False  # not a pytest test class despite the name


def _index_entry(app_dir: Path):
    found = find_index_for(app_dir)
    if found is None:
        return None, None
    root, index = found
    try:
        relative = app_dir.resolve().relative_to(root).as_posix()
    except ValueError:
        return root, None
    entry = next((e for e in index.apps if e.path == relative), None)
    return (root, index), entry


def classify(app_dir: str | Path) -> tuple[bool, str | None, object]:
    """(is_benchmark, split or None, (root, index) or None) for one application directory."""
    app_dir = Path(app_dir)
    located, entry = _index_entry(app_dir)
    split = entry.split if entry is not None else None
    is_benchmark = entry is not None
    if has_manifest(app_dir):
        manifest = load_manifest(app_dir)
        is_benchmark = is_benchmark or manifest.is_benchmark
        if split is None and manifest.split != "unassigned":
            split = manifest.split
    return is_benchmark, split, located


def check_access(app_dirs: list[str | Path], access: Access) -> None:
    for app_dir in app_dirs:
        is_benchmark, split, located = classify(app_dir)
        if access == "training" and is_benchmark:
            raise TestSetAccessError(
                f"{app_dir} is a benchmark application (split: {split or 'unassigned'}); benchmark apps must "
                "never be used for training or model selection"
            )
        if access == "diagnostic" and split == "test":
            raise TestSetAccessError(f"{app_dir} is in the locked/final test split; use the final evaluation path")
        if access == "final" and split == "test":
            if located is None or located[1].lock is None:
                raise TestSetAccessError(f"{app_dir}: final evaluation requires a locked benchmark index")


def record_final_run(index_root: str | Path, model_paths: list[str | Path], apps: list[str], note: str = "") -> Path:
    """Append one line per final evaluation: time, model hashes, evaluated apps."""
    log = Path(index_root) / FINAL_RUNS_LOG
    hashes = [f"{Path(m).name}={hashlib.sha256(Path(m).read_bytes()).hexdigest()[:16]}" for m in model_paths]
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\tmodels={','.join(hashes) or 'none'}\tapps={len(apps)}\t{note}\n"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return log
