"""Grouped, stratified dev/test split of benchmark applications, and the test-set lock.

Applications that must not be separated form one group: the same ``template_family``, a
``derived_from`` chain (a bad app derived from a good app), and optionally the same ``author``.
Whole groups are assigned to dev or test, aiming at ``dev_fraction`` of good apps and of bad apps
separately. The result is deterministic for a given seed and set of manifests.

Once ``lock`` has been run, the split cannot be recomputed and every later validation checks that
no test application changed.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from scaletrace.benchmark.loader import (
    ManifestError, file_sha256, labels_sha256, load_index, load_manifest, save_index, source_sha256, verification_state,
)
from scaletrace.benchmark.schema import MANIFEST_FILE, AppManifest, BenchmarkIndex, IndexEntry, Lock, LockedApp

DEFAULT_GROUP_BY = ["template_family", "derived_from"]
GROUP_FIELDS = {"template_family", "derived_from", "author"}


class SplitError(ValueError):
    pass


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def compute_groups(manifests: dict[str, AppManifest], group_by: list[str]) -> dict[str, str]:
    """app_id -> group label."""
    unknown = set(group_by) - GROUP_FIELDS
    if unknown:
        raise SplitError(f"unknown group_by fields {sorted(unknown)}; use {sorted(GROUP_FIELDS)}")
    uf = _UnionFind(sorted(manifests))
    by_value: dict[tuple[str, str], str] = {}
    for app_id, manifest in sorted(manifests.items()):
        origin = manifest.origin
        if "derived_from" in group_by and origin.derived_from in manifests:
            uf.union(app_id, origin.derived_from)
        for field in ("template_family", "author"):
            value = getattr(origin, field)
            if field in group_by and value:
                key = (field, value)
                if key in by_value:
                    uf.union(app_id, by_value[key])
                else:
                    by_value[key] = app_id
    return {app_id: f"group:{uf.find(app_id)}" for app_id in manifests}


def assign_splits(manifests: dict[str, AppManifest], dev_fraction: float, seed: int, group_by: list[str]) -> dict[str, str]:
    if not 0.0 <= dev_fraction < 1.0:
        raise SplitError("dev_fraction must be in [0, 1)")
    groups = compute_groups(manifests, group_by)
    members: dict[str, list[str]] = {}
    for app_id, group in groups.items():
        if manifests[app_id].split != "excluded":
            members.setdefault(group, []).append(app_id)
    totals = {kind: sum(1 for a in manifests.values() if a.kind == kind and a.split != "excluded") for kind in ("good", "bad")}
    targets = {kind: round(dev_fraction * n) for kind, n in totals.items()}

    order = sorted(members)
    random.Random(seed).shuffle(order)
    dev = {"good": 0, "bad": 0}
    splits: dict[str, str] = {a: "excluded" for a, m in manifests.items() if m.split == "excluded"}
    for group in order:
        counts = {kind: sum(1 for a in members[group] if manifests[a].kind == kind) for kind in ("good", "bad")}
        fits = all(dev[k] + counts[k] <= targets[k] for k in counts) and any(counts[k] and dev[k] < targets[k] for k in counts)
        split = "dev" if fits else "test"
        if fits:
            for kind in dev:
                dev[kind] += counts[kind]
        for app_id in members[group]:
            splits[app_id] = split
    return splits


def build_index(root: Path, apps: dict[str, tuple[Path, AppManifest]], dev_fraction: float, seed: int, group_by: list[str]) -> BenchmarkIndex:
    existing = load_index(root)
    if existing is not None and existing.lock is not None:
        raise SplitError(f"{root} is locked (since {existing.lock.locked_at}); the split cannot be recomputed")
    manifests = {app_id: manifest for app_id, (_, manifest) in apps.items()}
    groups = compute_groups(manifests, group_by)
    splits = assign_splits(manifests, dev_fraction, seed, group_by)
    entries = [
        IndexEntry(app_id=app_id, path=path.relative_to(root).as_posix(), kind=manifest.kind, split=splits[app_id], group=groups[app_id])
        for app_id, (path, manifest) in sorted(apps.items())
    ]
    return BenchmarkIndex(seed=seed, dev_fraction=dev_fraction, group_by=group_by, apps=entries)


def lock_index(root: Path, index: BenchmarkIndex) -> BenchmarkIndex:
    if index.lock is not None:
        raise SplitError(f"already locked at {index.lock.locked_at}")
    tests = [e for e in index.apps if e.split == "test"]
    if not tests:
        raise SplitError("no test applications to lock")
    locked = {}
    for entry in tests:
        app_dir = root / entry.path
        if not (app_dir / MANIFEST_FILE).exists():
            raise ManifestError(f"{entry.app_id}: manifest missing at {app_dir}")
        manifest = load_manifest(app_dir)
        locked[entry.app_id] = LockedApp(manifest_sha256=file_sha256(app_dir / MANIFEST_FILE), source_sha256=source_sha256(app_dir),
                                         labels_sha256=labels_sha256(manifest), verification_at_lock=verification_state(manifest))
    index.lock = Lock(locked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), apps=locked)
    save_index(root, index)
    return index
