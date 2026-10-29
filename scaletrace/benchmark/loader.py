"""Reading manifests and indexes, hashing application sources, and converting labels.

Two conversions keep the rest of ScaleTrace unchanged:

* ``groundtruth_from_manifest`` -> the detection ground truth used by ``scaletrace evaluate``;
* ``ml_labels_from_manifest`` -> the root-cause label dict used by ``scaletrace.ml``.

Positives depend on the verification level (reported separately, never mixed):

* ``source``  -- static verification only (a person confirmed the pattern in the code);
* ``runtime`` -- static verified AND runtime verified or not applicable (the default).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError

from scaletrace.benchmark.schema import INDEX_FILE, MANIFEST_FILE, REVIEW_COPY, AppManifest, BenchmarkIndex, Defect
from scaletrace.evaluation.groundtruth import ExpectedFinding, GroundTruth

HASH_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "env"}
HASHED_SUFFIXES = {".py", ".json", ".toml", ".cfg", ".ini", ".txt"}
INDEX_SEARCH_DEPTH = 4


class ManifestError(ValueError):
    """A manifest or index file is missing or invalid."""


def load_manifest(app_dir: str | Path) -> AppManifest:
    path = Path(app_dir) / MANIFEST_FILE
    try:
        return AppManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise ManifestError(f"{path}: cannot read ({exc.strerror})") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: invalid JSON ({exc})") from exc
    except ValidationError as exc:
        raise ManifestError(f"{path}: invalid manifest: {exc}") from exc


def has_manifest(app_dir: str | Path) -> bool:
    return (Path(app_dir) / MANIFEST_FILE).is_file()


def find_manifest_dirs(root: str | Path) -> list[Path]:
    """The root itself if it is an application, otherwise every direct or nested application below it."""
    root = Path(root)
    if has_manifest(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in HASH_SKIP_DIRS)
        if MANIFEST_FILE in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []  # applications do not nest
    return sorted(found)


def load_index(root: str | Path) -> BenchmarkIndex | None:
    path = Path(root) / INDEX_FILE
    if not path.exists():
        return None
    try:
        return BenchmarkIndex.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ManifestError(f"{path}: invalid index: {exc}") from exc


def save_index(root: str | Path, index: BenchmarkIndex) -> Path:
    path = Path(root) / INDEX_FILE
    path.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def find_index_for(app_dir: str | Path) -> tuple[Path, BenchmarkIndex] | None:
    """The nearest benchmark index in ``app_dir`` or one of its parents (a few levels up)."""
    current = Path(app_dir).resolve()
    for _ in range(INDEX_SEARCH_DEPTH):
        index = load_index(current)
        if index is not None:
            return current, index
        if current.parent == current:
            break
        current = current.parent
    return None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def labels_sha256(manifest: AppManifest) -> str:
    """Hash of what the labels say: everything except verification states and free-text notes.

    Frozen by the lock. Excluded: ``description`` and ``run`` (provenance text rewritten by apply-review) and each
    defect's ``verification`` (who confirmed it and when), which a second person may add after the lock.
    """
    data = manifest.model_dump(mode="json", exclude={"description", "run"})
    for defect in data["defects"]:
        defect.pop("verification", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def verification_state(manifest: AppManifest) -> dict[str, dict]:
    """defect_id -> the verification fields a sign-off may change."""
    return {d.defect_id: {"static": d.verification.static, "runtime": d.verification.runtime,
                          "verified_by": d.verification.verified_by} for d in manifest.defects}


def source_sha256(app_dir: str | Path) -> str:
    """Hash of every source/config file in the application (paths and contents), excluding caches."""
    app_dir = Path(app_dir)
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in HASH_SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix not in HASHED_SUFFIXES or name in (MANIFEST_FILE, REVIEW_COPY):
                continue
            digest.update(path.relative_to(app_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def positive_defects(manifest: AppManifest, level: str = "runtime") -> list[Defect]:
    return [d for d in manifest.defects if d.verification.at_level(level) and d.verification.static != "rejected"]


def unverified_defects(manifest: AppManifest, level: str = "runtime") -> list[Defect]:
    return [d for d in manifest.defects if not d.verification.at_level(level) and not d.verification.rejected]


def groundtruth_from_manifest(manifest: AppManifest, rules_in_scope: list[str], level: str = "runtime") -> GroundTruth:
    """Detection ground truth with the defects verified at ``level`` as expected findings."""
    return GroundTruth(
        groundtruth_version="manifest-" + manifest.schema_version,
        application=manifest.app_id,
        description=manifest.description,
        rules_in_scope=rules_in_scope,
        expected_findings=[
            ExpectedFinding(
                rule_id=d.rule_id, file=d.file, function=d.function, line=d.line_start,
                line_end=d.line_end, note=d.defect_id,
            )
            for d in positive_defects(manifest, level)
        ],
    )


def ml_labels_from_manifest(manifest: AppManifest, level: str = "runtime") -> dict:
    """Root-cause labels in the format used by ``scaletrace.ml`` (defects verified at ``level`` with a symptom)."""
    symptoms = [
        {
            "endpoint": d.endpoint,
            "kind": d.symptom_kind,
            "defect": d.rule_id,
            "root_cause_functions": list(d.root_cause_functions),
            "defect_id": d.defect_id,
        }
        for d in positive_defects(manifest, level)
        if d.endpoint and d.symptom_kind and d.root_cause_functions
    ]
    return {"rootcause_version": "manifest-" + manifest.schema_version, "verification_level": level, "symptoms": symptoms}
