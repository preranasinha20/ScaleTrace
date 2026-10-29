"""ZIP upload: safe extraction, limits, unsupported archives, cleanup, and real end-to-end analysis."""

from __future__ import annotations

import dataclasses
import io
import stat
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from scaletrace.api import server, uploads  # noqa: E402
from scaletrace.api.uploads import UploadError, UploadLimits, extract  # noqa: E402
from scaletrace.pipeline import run_static_analysis  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SHOP = REPO / "examples" / "shop_app"
APP = "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/users')\ndef users(db):\n    return db.query(User).all()\n"


def make_zip(files: dict[str, bytes | str], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def zip_of(folder: Path, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, prefix + path.relative_to(folder).as_posix())
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    base = tmp_path / "upload-tmp"
    base.mkdir()
    monkeypatch.setattr(server, "UPLOAD_TMP", str(base))
    monkeypatch.setattr(server, "uploads", server.UploadStore())
    c = TestClient(server.app)
    c.tmp_base = base  # type: ignore[attr-defined]
    return c


def post(client, data: bytes, **params):
    return client.post("/api/uploads", params={"filename": "project.zip", **params}, content=data,
                       headers={"Content-Type": "application/zip"})


def assert_clean(client) -> None:
    assert list(client.tmp_base.iterdir()) == [], "temporary upload directory was not removed"


# --- end-to-end -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["", "shop_app-main/"])
def test_upload_matches_local_static_analysis(client, prefix):
    response = post(client, zip_of(SHOP, prefix))
    assert response.status_code == 200, response.text
    data = response.json()
    local = run_static_analysis(SHOP).report
    assert {f["finding_id"] for f in data["findings"]} == {f.finding_id for f in local.findings}
    assert data["summary"]["files_analyzed"] == local.files_analyzed
    assert all(f["status"] == "static_candidate" for f in data["findings"])  # static only for uploads
    assert data["runtime"]["used"] is False and data["runtime"]["executes_code_from_api"] is False
    assert data["upload"]["project_root"] == prefix.rstrip("/")
    assert data["risk_paths"], "risk paths come from the real graph"
    assert_clean(client)


def test_upload_follow_up_requests_use_stored_results(client):
    data = post(client, zip_of(SHOP)).json()
    uid = data["upload"]["upload_id"]
    graph = client.get(f"/api/uploads/{uid}/graph").json()
    assert graph["nodes"] and all("level" in n for n in graph["nodes"])
    finding = data["findings"][0]
    excerpt = client.get(f"/api/uploads/{uid}/source", params={"file": finding["file"], "line": finding["line"]}).json()
    assert excerpt["highlight"] == finding["line"] and excerpt["lines"]
    for fmt, (filename, _) in server.EXPORTS.items():
        response = client.get(f"/api/uploads/{uid}/export/{fmt}")
        assert response.status_code == 200 and filename in response.headers["content-disposition"]
    ranked = client.post(f"/api/uploads/{uid}/analyze", params={"endpoint": "GET /customers/report"}).json()
    assert [d["symptom"]["endpoint"] for d in ranked["diagnoses"]] == ["GET /customers/report"]
    assert ranked["diagnoses"][0]["candidates"]
    assert client.get(f"/api/uploads/{uid}/source", params={"file": "app/main.py", "line": 99999}).status_code == 404
    assert client.delete(f"/api/uploads/{uid}").json() == {"deleted": True}
    assert client.get(f"/api/uploads/{uid}/graph").status_code == 404
    assert client.get("/api/uploads/not-a-real-id/graph").status_code == 404


def test_no_server_paths_leak(client):
    data = post(client, zip_of(SHOP)).json()
    uid = data["upload"]["upload_id"]
    texts = [str(data)] + [client.get(f"/api/uploads/{uid}/export/{fmt}").text for fmt in server.EXPORTS]
    for text in texts:
        assert str(client.tmp_base) not in text and "scaletrace-upload-" not in text
        assert str(REPO) not in text


def test_uploaded_code_is_never_executed(client, tmp_path):
    marker = tmp_path / "executed.txt"
    payload = f"from fastapi import FastAPI\nopen({str(marker)!r}, 'w').write('ran')\napp = FastAPI()\n"
    conftest = f"open({str(marker)!r}, 'w').write('ran')\n"
    response = post(client, make_zip({"app/main.py": payload, "app/__init__.py": payload, "conftest.py": conftest,
                                      "setup.py": conftest}))
    assert response.status_code == 200, response.text
    assert not marker.exists()
    assert_clean(client)


def test_unknown_model_rejected_before_reading(client):
    assert post(client, zip_of(SHOP), model="../../evil").status_code == 404


# --- malformed and unsupported archives -----------------------------------------------------------------------

@pytest.mark.parametrize("body", [b"", b"not a zip at all", b"PK\x03\x04" + b"\x00" * 40])
def test_malformed_archives(client, body):
    response = post(client, body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_archive"
    assert_clean(client)


def test_truncated_archive(client):
    data = make_zip({"app.py": APP})
    response = post(client, data[: len(data) // 2])
    assert response.status_code == 400
    assert_clean(client)


def test_no_python_files(client):
    response = post(client, make_zip({"README.md": "hello", "data/x.json": "{}"}))
    assert response.status_code == 422 and response.json()["code"] == "no_python"
    assert_clean(client)


def test_python_but_not_fastapi(client):
    response = post(client, make_zip({"tool.py": "import flask\nprint('hi')\n"}))
    assert response.status_code == 422 and response.json()["code"] == "not_fastapi"


def test_empty_zip(client):
    response = post(client, make_zip({}))
    assert response.status_code == 422 and response.json()["code"] == "no_python"


def test_encrypted_member_rejected(client):
    # zipfile cannot write encrypted members, so set the "encrypted" bit (0x1) of the general-purpose flags in
    # the local header (offset 6) and the central-directory header (offset 8) by hand.
    data = bytearray(make_zip({"app.py": APP}, compression=zipfile.ZIP_STORED))
    local, central = data.find(b"PK\x03\x04"), data.find(b"PK\x01\x02")
    data[local + 6] |= 0x1
    data[central + 8] |= 0x1
    assert zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo("app.py").flag_bits & 0x1
    response = post(client, bytes(data))
    assert response.status_code == 400 and "encrypted" in response.json()["detail"]


# --- traversal, symlinks, special files, names -----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../evil.py", "app/../../evil.py", "/etc/evil.py", "C:/evil.py", "c:evil.py", "app\\..\\evil.py",
    "app/evil.py\x00.txt", "app/CON.py", "app/nul", "app/trailing. /x.py", "app/dot./x.py",
])
def test_unsafe_member_names_rejected(client, tmp_path, name):
    data = make_zip({"app/main.py": APP, name: "print('x')\n"}) if "\x00" not in name else None
    if data is None:  # zipfile refuses NUL in names; check the validator directly
        with pytest.raises(UploadError):
            uploads.check_member_name(name, UploadLimits())
        return
    response = post(client, data)
    assert response.status_code == 400, (name, response.text)
    assert "evil" not in str(response.json())  # the message does not echo raw member names
    assert not (tmp_path / "evil.py").exists() and not (tmp_path.parent / "evil.py").exists()
    assert_clean(client)


def _with_mode(name: str, mode: int, data: bytes = b"target") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app/main.py", APP)
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = mode << 16
        z.writestr(info, data)
    return buf.getvalue()


def test_symlink_rejected(client):
    response = post(client, _with_mode("app/link.py", stat.S_IFLNK | 0o777, b"/etc/passwd"))
    assert response.status_code == 400 and "symbolic link" in response.json()["detail"]
    assert_clean(client)


def test_special_file_rejected(client):
    response = post(client, _with_mode("app/fifo.py", stat.S_IFIFO | 0o644))
    assert response.status_code == 400 and "special file" in response.json()["detail"]


def test_duplicate_names_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app/main.py", APP)
        z.writestr("app/Main.py", APP)  # same file on case-insensitive file systems
    response = post(client, buf.getvalue())
    assert response.status_code == 400 and "twice" in response.json()["detail"]


def test_only_source_and_config_files_are_written(tmp_path):
    destination = tmp_path / "out"
    destination.mkdir()
    data = make_zip({"app/main.py": APP, "pyproject.toml": "[project]\n", "bin/tool.exe": b"MZ" * 100,
                     "nested.zip": make_zip({"x.py": "print(1)"}), "static/logo.png": b"\x89PNG" * 10})
    result = extract(data, destination, UploadLimits())
    written = sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file())
    assert written == ["app/main.py", "pyproject.toml"]
    assert result.members_skipped == 3 and result.python_files == 1


# --- resource limits -------------------------------------------------------------------------------------------

def limits(monkeypatch, **changes) -> None:
    monkeypatch.setattr(server, "UPLOAD_LIMITS", dataclasses.replace(UploadLimits(), **changes))


def test_upload_size_limit(client, monkeypatch):
    limits(monkeypatch, max_upload_bytes=1024)
    response = post(client, make_zip({"app/main.py": APP + "#" + "x" * 4000}, compression=zipfile.ZIP_STORED))
    assert response.status_code == 413 and response.json()["code"] == "upload_too_large"
    assert_clean(client)


def test_member_count_limit(client, monkeypatch):
    limits(monkeypatch, max_members=5)
    response = post(client, make_zip({"app/main.py": APP, **{f"app/m{i}.py": "x = 1\n" for i in range(10)}}))
    assert response.status_code == 413 and response.json()["code"] == "too_many_files"


def test_nesting_limit(client, monkeypatch):
    limits(monkeypatch, max_depth=4)
    response = post(client, make_zip({"app/main.py": APP, "a/b/c/d/e/f.py": "x = 1\n"}))
    assert response.status_code == 400 and "levels deep" in response.json()["detail"]


def test_per_file_limit(client, monkeypatch):
    limits(monkeypatch, max_file_bytes=1000)
    response = post(client, make_zip({"app/main.py": APP + "#" * 5000}))
    assert response.status_code == 413 and response.json()["code"] == "file_too_large"


def test_total_extracted_limit(client, monkeypatch):
    limits(monkeypatch, max_extracted_bytes=3000)
    response = post(client, make_zip({"app/main.py": APP, **{f"app/m{i}.py": "#" * 900 for i in range(5)}}))
    assert response.status_code == 413 and response.json()["code"] == "too_large_extracted"
    assert_clean(client)


def test_zip_bomb_ratio(client):
    bomb = make_zip({"app/main.py": APP, "app/bomb.py": b"#" * (1_500_000)})  # compresses roughly 1000:1
    response = post(client, bomb)
    assert response.status_code == 413 and response.json()["code"] == "zip_bomb"
    assert_clean(client)


def test_lying_header_is_rejected(tmp_path):
    """A member whose real size exceeds its declared size is rejected. zipfile itself stops at the declared size
    and then fails the CRC check; extract()'s own count of decompressed bytes is a second, independent guard."""
    data = bytearray(make_zip({"app/main.py": APP, "app/big.py": b"#" * 200_000}))
    # Patch the declared uncompressed size of app/big.py (central directory and local header) to 10 bytes.
    archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    info = archive.getinfo("app/big.py")
    real = info.file_size.to_bytes(4, "little")
    fake = (10).to_bytes(4, "little")
    data = bytes(data).replace(real, fake)
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(UploadError):
        extract(data, destination, UploadLimits())


# --- cleanup ---------------------------------------------------------------------------------------------------

def test_cleanup_after_analysis_failure(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret internal detail C:\\server\\path")

    monkeypatch.setattr(server, "run_static_analysis", boom)
    response = post(client, make_zip({"app/main.py": APP}))
    assert response.status_code == 500 and response.json()["code"] == "analysis_failed"
    assert "secret" not in response.text and "server" not in response.json()["detail"].lower().replace("scaletrace", "")
    assert_clean(client)


def test_busy_server_returns_429(client, monkeypatch):
    monkeypatch.setattr(server, "_upload_slots", server.BoundedSemaphore(1))
    server._upload_slots.acquire()
    try:
        assert post(client, make_zip({"app/main.py": APP})).status_code == 429
    finally:
        server._upload_slots.release()


def test_store_expiry_and_capacity(client, monkeypatch):
    monkeypatch.setattr(server, "UPLOAD_MAX_STORED", 2)
    ids = [post(client, make_zip({"app/main.py": APP})).json()["upload"]["upload_id"] for _ in range(3)]
    assert client.get(f"/api/uploads/{ids[0]}/graph").status_code == 404  # evicted (oldest)
    assert client.get(f"/api/uploads/{ids[2]}/graph").status_code == 200
    monkeypatch.setattr(server.time, "time", lambda: 10**12)  # far future: everything expired
    assert client.get(f"/api/uploads/{ids[2]}/graph").status_code == 404


def test_config_endpoint(client):
    data = client.get("/api/uploads/config").json()
    assert data["max_upload_bytes"] == server.UPLOAD_LIMITS.max_upload_bytes and data["ttl_seconds"] > 0
