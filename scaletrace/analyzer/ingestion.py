"""Repository ingestion: find the Python sources to analyze without executing anything."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from scaletrace.rules.config import DetectorConfig
from scaletrace.schemas import SkippedFile

# Only the names of these files are recorded; their contents are not read, so secrets in
# e.g. .env files can never leak into a report.
CONFIG_FILE_NAMES = {"pyproject.toml", "setup.cfg", "setup.py", "Pipfile", "alembic.ini"}


class RepositoryError(ValueError):
    """The given path is not an analyzable repository."""


@dataclass(frozen=True)
class SourceFile:
    path: Path
    rel_path: str  # POSIX-style, relative to the repository root


@dataclass
class RepositoryInventory:
    root: Path
    python_files: list[SourceFile] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)


TEST_DIR_NAMES = {"tests", "test"}


def _is_excluded_dir(path: Path, name: str, config: DetectorConfig) -> bool:
    return (
        name.startswith(".")
        or name in config.excluded_dirs
        or name.endswith(".egg-info")
        or (config.exclude_tests and name in TEST_DIR_NAMES)
        or (path / "pyvenv.cfg").exists()
    )


def _is_test_file(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def discover_repository(root: str | Path, config: DetectorConfig | None = None) -> RepositoryInventory:
    """List Python files and recognised config files under ``root``.

    Hidden directories, virtual environments, caches and build output are skipped. Symlinks
    are not followed, so analysis never escapes the repository directory.
    """
    config = config or DetectorConfig()
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise RepositoryError(f"not a directory: {root}")

    inventory = RepositoryInventory(root=root_path)
    for dirpath, dirnames, filenames in os.walk(root_path):
        current = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(current / d, d, config))

        for name in sorted(filenames):
            path = current / name
            rel_path = path.relative_to(root_path).as_posix()
            if name in CONFIG_FILE_NAMES or (name.startswith("requirements") and name.endswith(".txt")):
                inventory.config_files.append(rel_path)
            if not name.endswith(".py") or (config.exclude_tests and _is_test_file(name)):
                continue
            if path.is_symlink():
                inventory.skipped.append(SkippedFile(file=rel_path, reason="symbolic link not followed"))
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                inventory.skipped.append(SkippedFile(file=rel_path, reason=f"unreadable: {exc.strerror}"))
                continue
            if size > config.max_file_size_bytes:
                inventory.skipped.append(
                    SkippedFile(file=rel_path, reason=f"file larger than {config.max_file_size_bytes} bytes")
                )
                continue
            inventory.python_files.append(SourceFile(path=path, rel_path=rel_path))
    return inventory
