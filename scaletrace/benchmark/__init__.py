"""Benchmark manifests, validation, dev/test splits, test-set lock and access guard."""

from scaletrace.benchmark.guard import TestSetAccessError, check_access, record_final_run
from scaletrace.benchmark.loader import (
    ManifestError,
    find_manifest_dirs,
    groundtruth_from_manifest,
    has_manifest,
    load_index,
    load_manifest,
    ml_labels_from_manifest,
)
from scaletrace.benchmark.schema import INDEX_FILE, MANIFEST_FILE, AppManifest, BenchmarkIndex, Defect
from scaletrace.benchmark.validate import ValidationReport, validate_benchmark

__all__ = [
    "INDEX_FILE",
    "MANIFEST_FILE",
    "AppManifest",
    "BenchmarkIndex",
    "Defect",
    "ManifestError",
    "TestSetAccessError",
    "ValidationReport",
    "check_access",
    "find_manifest_dirs",
    "groundtruth_from_manifest",
    "has_manifest",
    "load_index",
    "load_manifest",
    "ml_labels_from_manifest",
    "record_final_run",
    "validate_benchmark",
]
