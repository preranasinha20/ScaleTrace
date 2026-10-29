"""Safe extraction of uploaded ZIP archives for static analysis.

Uploaded code is untrusted. This module only reads the archive with :mod:`zipfile` and writes the selected
member files as plain bytes; it never imports, runs or installs anything from the archive.

Defences (all limits are configurable, see :class:`UploadLimits`):

- size of the upload itself (checked while it streams in, before anything is parsed);
- archive validity (bad or truncated archives, unsupported compression, encrypted members);
- member names: no absolute paths, drive letters, backslashes, NUL, ``..`` components or duplicate names
  (case-insensitive, so Windows cannot merge two members), and a maximum nesting depth;
- member types: symbolic links and other special files are rejected; directories are only created implicitly;
- resource use: number of members, size of each extracted file, total extracted size and the compression
  ratio, each checked against the archive's declared sizes AND against the bytes actually decompressed;
- only Python source and the config files the pipeline reads are written. Everything else (binaries,
  nested archives, data) is counted and skipped, never decompressed;
- every target path is resolved and must stay inside the extraction directory.

Errors are :class:`UploadError` with a short user-facing message that never contains server paths.
"""

from __future__ import annotations

import io
import os
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scaletrace.analyzer.ingestion import CONFIG_FILE_NAMES

CHUNK = 64 * 1024
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # local file header; empty archive (end of central directory)
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_FASTAPI = re.compile(r"^\s*(from\s+fastapi(\.\w+)*\s+import|import\s+fastapi)\b", re.MULTILINE)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class UploadLimits:
    max_upload_bytes: int = 20 * 1024 * 1024
    max_extracted_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 2_000_000  # same as the pipeline's max_file_size_bytes
    max_members: int = 5_000
    max_depth: int = 20
    max_ratio: int = 100  # uncompressed / compressed, per member

    @classmethod
    def from_env(cls) -> UploadLimits:
        d = cls()
        return cls(
            max_upload_bytes=_env_int("SCALETRACE_UPLOAD_MAX_BYTES", d.max_upload_bytes),
            max_extracted_bytes=_env_int("SCALETRACE_UPLOAD_MAX_EXTRACTED_BYTES", d.max_extracted_bytes),
            max_file_bytes=_env_int("SCALETRACE_UPLOAD_MAX_FILE_BYTES", d.max_file_bytes),
            max_members=_env_int("SCALETRACE_UPLOAD_MAX_MEMBERS", d.max_members),
            max_depth=_env_int("SCALETRACE_UPLOAD_MAX_DEPTH", d.max_depth),
            max_ratio=_env_int("SCALETRACE_UPLOAD_MAX_RATIO", d.max_ratio),
        )


class UploadError(Exception):
    """A rejected upload. ``message`` is safe to show to the user; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_archive") -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


@dataclass
class Extraction:
    root: Path  # the detected project root (inside the extraction directory)
    project_root: str  # the root as a path inside the archive, "" for the archive's top level
    python_files: int = 0
    files_written: int = 0
    bytes_written: int = 0
    members_skipped: int = 0
    skipped_examples: list[str] = field(default_factory=list)


def safe_stem(filename: str | None) -> str:
    stem = Path(filename or "upload.zip").name
    stem = stem[:-4] if stem.lower().endswith(".zip") else stem
    stem = _SAFE_STEM.sub("-", stem).strip(".-")[:60]
    return stem or "upload"


def wanted(name: str) -> bool:
    """Files the static pipeline actually reads: Python source and recognised config files."""
    base = PurePosixPath(name).name
    return base.endswith(".py") or base in CONFIG_FILE_NAMES or (base.startswith("requirements") and base.endswith(".txt"))


def check_member_name(name: str, limits: UploadLimits) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise UploadError("The archive contains a file name with invalid characters.")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadError("The archive contains an absolute path. Only relative paths are accepted.")
    path = PurePosixPath(name)
    parts = [p for p in path.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError("The archive contains a path that points outside the project ('..').")
    if any(":" in p or p.endswith((".", " ")) for p in parts):
        raise UploadError("The archive contains a file name with invalid characters.")
    if any(_WINDOWS_RESERVED.match(p) for p in parts):
        raise UploadError("The archive contains a reserved file name (such as CON, NUL or COM1).")
    if len(parts) > limits.max_depth:
        raise UploadError(f"The archive nests folders more than {limits.max_depth} levels deep.")
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _member_type_error(info: zipfile.ZipInfo) -> str | None:
    mode = info.external_attr >> 16
    if info.create_system == 3 and mode:  # Unix attributes are only meaningful when made on Unix
        if stat.S_ISLNK(mode):
            return "The archive contains a symbolic link. Symbolic links are not accepted."
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            return "The archive contains a special file (device, pipe or socket). Only regular files are accepted."
    if info.flag_bits & 0x1:
        return "The archive contains encrypted files. Upload an unencrypted ZIP."
    return None


def open_archive(data: bytes) -> zipfile.ZipFile:
    if not data:
        raise UploadError("The upload is empty.")
    if not data.startswith(ZIP_MAGIC):
        raise UploadError("The file is not a ZIP archive.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError):
        raise UploadError("The ZIP archive is damaged or incomplete.") from None
    return archive


def extract(data: bytes, destination: Path, limits: UploadLimits) -> Extraction:
    """Validate ``data`` as a ZIP and write its Python/config members under ``destination`` (an empty directory)."""
    archive = open_archive(data)
    with archive:
        members = archive.infolist()
        if len(members) > limits.max_members:
            raise UploadError(f"The archive has more than {limits.max_members} entries.", 413, "too_many_files")

        plan: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen: set[str] = set()
        declared = 0
        skipped: list[str] = []
        for info in members:
            path = check_member_name(info.filename, limits)
            if (error := _member_type_error(info)) is not None:
                raise UploadError(error)
            key = str(path).casefold()
            if key in seen and not info.is_dir():
                raise UploadError("The archive contains the same file name twice.")
            seen.add(key)
            if info.is_dir():
                continue
            if not wanted(str(path)):
                if len(skipped) < 5:
                    skipped.append(str(path))
                continue
            if info.file_size > limits.max_file_bytes:
                raise UploadError(f"A source file in the archive is larger than {limits.max_file_bytes} bytes.",
                                  413, "file_too_large")
            if info.compress_size and info.file_size / info.compress_size > limits.max_ratio and info.file_size > 1_000_000:
                raise UploadError("The archive is compressed suspiciously well (possible ZIP bomb).", 413, "zip_bomb")
            declared += info.file_size
            if declared > limits.max_extracted_bytes:
                raise UploadError(f"The extracted source would exceed {limits.max_extracted_bytes} bytes.",
                                  413, "too_large_extracted")
            plan.append((info, path))

        base = destination.resolve()
        result = Extraction(root=base, project_root="", members_skipped=sum(1 for i in members if not i.is_dir()) - len(plan),
                            skipped_examples=skipped)
        for info, path in plan:
            target = (base / Path(*path.parts)).resolve()
            if base not in target.parents:
                raise UploadError("The archive contains a path that points outside the project.")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with archive.open(info) as source, open(target, "xb") as sink:
                    while chunk := source.read(CHUNK):
                        written += len(chunk)
                        result.bytes_written += len(chunk)
                        # Never trust the declared sizes: count what is actually decompressed.
                        if written > limits.max_file_bytes or written > max(info.file_size, 0) + CHUNK:
                            raise UploadError("A file in the archive is larger than its header says.", 413, "zip_bomb")
                        if result.bytes_written > limits.max_extracted_bytes:
                            raise UploadError(f"The extracted source would exceed {limits.max_extracted_bytes} bytes.",
                                              413, "too_large_extracted")
                        sink.write(chunk)
            except UploadError:
                raise
            except FileExistsError:
                raise UploadError("The archive contains the same file name twice.") from None
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError, ValueError):
                raise UploadError("The ZIP archive is damaged, encrypted or uses an unsupported compression method.") from None
            result.files_written += 1
            if path.suffix == ".py":
                result.python_files += 1

    if result.python_files == 0:
        raise UploadError("The archive contains no Python files. Upload a ZIP of a Python/FastAPI project.",
                          422, "no_python")
    prefix = wrapper_prefix([p for _, p in plan] + [check_member_name(i.filename, limits) for i in members if not i.is_dir()])
    result.root = base.joinpath(*prefix.parts) if prefix.parts else base
    result.project_root = prefix.as_posix() if prefix.parts else ""
    if not uses_fastapi(result.root):
        raise UploadError("No FastAPI import was found in the archive's Python files. ScaleTrace analyses FastAPI "
                          "backends; upload the folder that contains the application.", 422, "not_fastapi")
    return result


def wrapper_prefix(paths: list[PurePosixPath]) -> PurePosixPath:
    """The project root inside the archive: strip wrapper folders (e.g. GitHub's ``repo-main/``) as long as every
    file in the archive, extracted or not, lies under one single folder. Uses all members, so a folder that
    only holds non-Python files at the top still counts as the root."""
    prefix: list[str] = []
    while True:
        heads = {p.parts[len(prefix)] if len(p.parts) > len(prefix) + 1 else None for p in paths}
        if len(heads) != 1 or None in heads:
            return PurePosixPath(*prefix) if prefix else PurePosixPath()
        prefix.append(heads.pop())


def uses_fastapi(root: Path, max_files: int = 2_000) -> bool:
    for count, path in enumerate(root.rglob("*.py")):
        if count >= max_files:
            break
        try:
            if _FASTAPI.search(path.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


async def read_limited(stream, limit: int) -> bytes:
    """Read a request body stream, stopping as soon as it exceeds ``limit`` bytes."""
    buffer = bytearray()
    async for chunk in stream:
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise UploadError(f"The upload is larger than the {limit // (1024 * 1024)} MB limit.", 413, "upload_too_large")
    return bytes(buffer)
