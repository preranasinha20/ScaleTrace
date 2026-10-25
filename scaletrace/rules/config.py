"""Detector configuration: the tunable vocabulary that static detectors match against.

Kept separate from the rulebook so the rulebook stays a stable research artifact, while
detector heuristics can be tuned (and the tuning documented) per experiment.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(ValueError):
    """The detector configuration file is missing or invalid."""


class DetectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- Repository ingestion -------------------------------------------------------------
    excluded_dirs: list[str] = Field(
        default=[
            "__pycache__", "venv", "env", "node_modules", "build", "dist",
            "site-packages", "htmlcov", "scaletrace_output",
        ],
        description="Directory names never descended into. Hidden directories (.git, .venv, ...) "
        "and directories containing pyvenv.cfg are always skipped.",
    )
    max_file_size_bytes: int = 2_000_000
    exclude_tests: bool = Field(
        default=True,
        description="Skip tests/ directories and test_*.py / *_test.py / conftest.py files; test code "
        "is not on a request path and would add noise.",
    )

    # --- Database handle inference ----------------------------------------------------------
    db_handle_types: list[str] = Field(
        default=["Session", "AsyncSession", "Connection", "AsyncConnection", "scoped_session", "Cursor"],
        description="Annotation type names that mark a parameter or variable as a database handle.",
    )
    db_handle_names: list[str] = Field(
        default=["db", "session", "db_session", "sess", "conn", "connection", "cursor", "cur"],
        description="Fallback: variable/attribute names treated as database handles when no "
        "stronger evidence exists. Reported as 'name_heuristic' in the evidence.",
    )
    db_dependency_pattern: str = Field(
        default=r"(?i)(db|session|conn)",
        description="Regex on the function passed to Depends(...) that marks a parameter as a DB handle.",
    )
    db_factory_pattern: str = Field(
        default=r"(?i)(session|^connect$|^cursor$|^begin$|^acquire$)",
        description="Regex on the called name whose result is a DB handle, e.g. SessionLocal(), "
        "engine.connect(). Names ending in 'maker' (sessionmaker) are excluded.",
    )
    non_db_factories: list[str] = Field(
        default=[
            "requests.Session", "requests.session", "httpx.Client", "httpx.AsyncClient",
            "aiohttp.ClientSession", "ClientSession", "AsyncClient",
        ],
        description="Dotted constructor names whose result is NOT a database handle (HTTP clients).",
    )
    non_db_modules: list[str] = Field(
        default=["requests", "httpx", "aiohttp"],
        description="Annotations from these modules never mark a database handle.",
    )
    db_operations: list[str] = Field(
        default=[
            "query", "execute", "exec", "executemany", "scalars", "scalar", "get", "stream",
            "stream_scalars", "refresh", "merge", "flush", "commit",
            "fetch", "fetchrow", "fetchval", "fetchone", "fetchall", "fetchmany",
        ],
        description="Methods on a DB handle that issue (or may issue) a database round trip.",
    )

    db_unbounded_fetches: list[str] = Field(
        default=["all", "fetchall"],
        description="MEM-001: methods that materialize a full result set when called on a DB chain.",
    )
    db_bounding_methods: list[str] = Field(
        default=[
            "limit", "slice", "paginate", "fetchmany", "first", "one", "one_or_none",
            "scalar_one", "scalar_one_or_none", "yield_per", "partitions",
        ],
        description="MEM-001: a DB chain containing one of these methods is treated as bounded.",
    )
    eager_loading_options: list[str] = Field(
        default=["selectinload", "joinedload", "subqueryload", "immediateload", "contains_eager"],
        description="DB-003: query options that load a relationship eagerly.",
    )
    eager_lazy_values: list[str] = Field(
        default=["joined", "selectin", "subquery", "immediate", "raise", "raise_on_sql", "noload"],
        description="DB-003: relationship(lazy=...) values that do not issue one query per parent.",
    )

    # --- External services --------------------------------------------------------------------
    http_modules: list[str] = Field(default=["requests", "httpx", "aiohttp"])
    http_client_factories: list[str] = Field(
        default=[
            "requests.Session", "requests.session", "httpx.Client", "httpx.AsyncClient",
            "aiohttp.ClientSession", "ClientSession", "AsyncClient",
        ],
        description="Constructors (dotted or bare) whose result is an HTTP client.",
    )
    http_client_types: list[str] = Field(
        default=["httpx.Client", "httpx.AsyncClient", "requests.Session", "aiohttp.ClientSession", "AsyncClient", "ClientSession"],
        description="Annotations that mark a parameter as an HTTP client.",
    )
    http_methods: list[str] = Field(
        default=["get", "post", "put", "patch", "delete", "head", "options", "request", "send", "stream"],
    )

    # --- Computation --------------------------------------------------------------------------
    expensive_functions: list[str] = Field(
        default=[
            "sorted", "re.compile", "json.loads", "json.dumps", "copy.deepcopy", "deepcopy",
            "pickle.loads", "pickle.dumps", "yaml.load", "yaml.safe_load", "hashlib.pbkdf2_hmac",
            "bcrypt.hashpw", "bcrypt.checkpw", "pd.read_csv", "pandas.read_csv", "pd.DataFrame",
            "pandas.DataFrame", "ET.fromstring", "xml.etree.ElementTree.fromstring", "ast.parse",
        ],
        description="CMP-001: calls (dotted or bare name) considered potentially expensive.",
    )
    expensive_methods: list[str] = Field(
        default=["sort", "predict", "predict_proba", "fit", "transform", "encode_batch"],
        description="CMP-001: method names considered potentially expensive on any receiver.",
    )

    # --- Memory -------------------------------------------------------------------------------
    collection_growth_methods: list[str] = Field(
        default=["append", "extend", "add", "update", "insert", "setdefault", "appendleft", "extendleft"],
        description="MEM-002: methods that grow a collection.",
    )
    collection_bounding_methods: list[str] = Field(
        default=["clear", "pop", "popitem", "popleft", "remove", "discard"],
        description="MEM-002: a visible call of one of these on the collection counts as a bound.",
    )

    # --- Loop analysis ----------------------------------------------------------------------
    bounded_loop_max_iterations: int = Field(
        default=10,
        description="Loops over literal collections or constant range() of at most this many "
        "iterations are treated as strictly bounded and not reported.",
    )


def load_config(path: str | Path | None = None) -> DetectorConfig:
    """Load a detector config JSON file; missing keys fall back to defaults."""
    if path is None:
        return DetectorConfig()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return DetectorConfig.model_validate(data)
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ConfigError(f"invalid config: {exc}") from exc
