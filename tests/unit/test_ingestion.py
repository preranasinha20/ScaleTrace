from __future__ import annotations

import pytest

from scaletrace.analyzer.ingestion import RepositoryError, discover_repository
from scaletrace.pipeline import analyze_repository


def _touch(path, text="x = 1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_excludes_venvs_caches_and_hidden_dirs(tmp_path):
    _touch(tmp_path / "app" / "main.py")
    _touch(tmp_path / ".venv" / "lib" / "x.py")
    _touch(tmp_path / ".git" / "hooks" / "x.py")
    _touch(tmp_path / "__pycache__" / "x.py")
    _touch(tmp_path / "pkg.egg-info" / "x.py")
    _touch(tmp_path / "myenv" / "pyvenv.cfg", "home = /usr\n")
    _touch(tmp_path / "myenv" / "lib" / "x.py")

    inventory = discover_repository(tmp_path)
    assert [f.rel_path for f in inventory.python_files] == ["app/main.py"]


def test_records_config_file_names_only(tmp_path):
    _touch(tmp_path / "pyproject.toml", "[project]\n")
    _touch(tmp_path / "requirements-dev.txt", "pytest\n")
    _touch(tmp_path / ".env", "SECRET=1\n")

    inventory = discover_repository(tmp_path)
    assert inventory.config_files == ["pyproject.toml", "requirements-dev.txt"]


def test_not_a_directory(tmp_path):
    with pytest.raises(RepositoryError):
        discover_repository(tmp_path / "missing")


def test_syntax_errors_are_skipped_not_fatal(tmp_path):
    _touch(tmp_path / "broken.py", "def f(:\n")
    _touch(tmp_path / "ok.py", "def g(db, ids):\n    for i in ids:\n        db.get(User, i)\n")

    report = analyze_repository(tmp_path)
    assert report.files_analyzed == 1
    assert [s.file for s in report.files_skipped] == ["broken.py"]
    assert "syntax error at line 1" in report.files_skipped[0].reason
    assert [f.file for f in report.findings] == ["ok.py"]


def test_oversized_file_skipped(tmp_path):
    from scaletrace.rules import DetectorConfig

    _touch(tmp_path / "big.py", "x = 1\n" * 100)
    inventory = discover_repository(tmp_path, DetectorConfig(max_file_size_bytes=10))
    assert inventory.python_files == []
    assert "larger than" in inventory.skipped[0].reason


def test_report_records_no_absolute_paths(tmp_path):
    _touch(tmp_path / "ok.py", "def g(db, ids):\n    for i in ids:\n        db.get(User, i)\n")
    report_json = analyze_repository(tmp_path).model_dump_json()
    assert str(tmp_path) not in report_json
    assert str(tmp_path).replace("\\", "/") not in report_json
