"""ScaleTrace runtime bootstrap: serve a target ASGI app under instrumentation.

THIS EXECUTES TARGET CODE. It is started only by ``scaletrace run --allow-execute``, in a
separate process bound to 127.0.0.1.

Standalone on purpose (standard library only, plus the target's own uvicorn/SQLAlchemy), so it
can run in the target application's interpreter without ScaleTrace being installed there.

Per HTTP request it records:
* status and server-side duration;
* SQL statements via SQLAlchemy ``Engine`` events: count, time, normalized text, and the
  innermost project function on the stack when each statement ran (query attribution);
* optionally (``--trace-calls``), calls between project functions via ``sys.setprofile``
  (observed call edges). Profiling adds overhead, so absolute latencies are inflated.

Records are served (and cleared) at ``GET /__scaletrace__/records``. Only metrics are recorded:
no request bodies, headers, environment variables or query parameters.
"""

from __future__ import annotations

import argparse
import contextvars
import importlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter

CONTROL_PREFIX = "/__scaletrace__"
MAX_RECORDS = 50_000
GENERATOR_FLAGS = 0x20 | 0x80 | 0x200  # CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR
SKIPPED_DIR_PARTS = {"site-packages", "dist-packages", "__pycache__"}

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("scaletrace_request", default=None)
_records: list[dict] = []
_lock = threading.Lock()
_SQL_LITERALS = re.compile(r"'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")


def normalize_sql(statement: str) -> str:
    return " ".join(_SQL_LITERALS.sub("?", statement).split())[:300]


def module_name_for(rel_path: str) -> str:
    parts = rel_path[:-3].split("/") if rel_path.endswith(".py") else rel_path.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


class Locator:
    """Maps code objects to ScaleTrace function ids (``module:qualname``) for project files."""

    def __init__(self, root: str) -> None:
        self.root = os.path.normcase(os.path.abspath(root)) + os.sep
        self._files: dict[str, str | None] = {}
        self._codes: dict[object, str | None] = {}

    def _module(self, filename: str) -> str | None:
        if filename in self._files:
            return self._files[filename]
        module = None
        norm = os.path.normcase(os.path.abspath(filename))
        if norm.startswith(self.root) and norm.endswith(".py"):
            rel = norm[len(self.root):].replace(os.sep, "/")
            parts = rel.split("/")
            if not any(p.startswith(".") or p in SKIPPED_DIR_PARTS for p in parts[:-1]):
                module = module_name_for(rel)
        self._files[filename] = module
        return module

    def func_id(self, code) -> str | None:
        try:
            return self._codes[code]
        except KeyError:
            pass
        result = None
        module = self._module(code.co_filename)
        if module is not None:
            name = getattr(code, "co_qualname", code.co_name)
            if name == "<module>" or not name.startswith("<"):
                if not name.rsplit(".", 1)[-1].startswith("<") or name == "<module>":
                    result = f"{module}:{name.replace('.<locals>', '')}"
        self._codes[code] = result
        return result

    def innermost(self, frame) -> str | None:
        while frame is not None:
            found = self.func_id(frame.f_code)
            if found is not None:
                return found
            frame = frame.f_back
        return None


def make_profiler(locator: Locator):
    def profile(frame, event, arg):
        if event != "call":
            return
        record = _current.get()
        if record is None:
            return
        code = frame.f_code
        callee = locator.func_id(code)
        if callee is None:
            return
        if code.co_flags & GENERATOR_FLAGS:
            # Generators/coroutines emit "call" on every resume; count the first entry only.
            key = id(frame)
            if key in record["seen_frames"]:
                return
            record["seen_frames"].add(key)
        caller = locator.innermost(frame.f_back) or "<request>"
        record["calls"][(caller, callee)] += 1
        record["functions"][callee] += 1

    return profile


def install_sql_hooks(locator: Locator) -> bool:
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except Exception:  # SQLAlchemy not used by the target
        return False

    def before(conn, cursor, statement, parameters, context, executemany):
        record = _current.get()
        if record is None:
            return
        conn.info.setdefault("_scaletrace_t0", []).append(time.perf_counter())
        record["queries"] += 1
        record["statements"][normalize_sql(statement)] += 1
        owner = locator.innermost(sys._getframe(1)) or "<unattributed>"
        record["queries_by_function"][owner] += 1

    def after(conn, cursor, statement, parameters, context, executemany):
        record = _current.get()
        stack = conn.info.get("_scaletrace_t0")
        if record is None or not stack:
            return
        record["query_time_ms"] += (time.perf_counter() - stack.pop()) * 1000.0

    event.listen(Engine, "before_cursor_execute", before)
    event.listen(Engine, "after_cursor_execute", after)
    return True


def _serializable(record: dict) -> dict:
    return {
        "method": record["method"],
        "path": record["path"],
        "status": record["status"],
        "duration_ms": record["duration_ms"],
        "queries": record["queries"],
        "query_time_ms": record["query_time_ms"],
        "statements": dict(record["statements"]),
        "queries_by_function": dict(record["queries_by_function"]),
        "functions": dict(record["functions"]),
        "calls": [[caller, callee, n] for (caller, callee), n in record["calls"].items()],
    }


class InstrumentedApp:
    def __init__(self, app, status: dict) -> None:
        self.app = app
        self.status = status

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path", "").startswith(CONTROL_PREFIX):
            return await self._control(scope, send)

        record = {
            "method": scope.get("method", ""),
            "path": scope.get("path", ""),
            "status": None,
            "duration_ms": 0.0,
            "queries": 0,
            "query_time_ms": 0.0,
            "statements": Counter(),
            "queries_by_function": Counter(),
            "functions": Counter(),
            "calls": Counter(),
            "seen_frames": set(),
        }

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
            await send(message)

        token = _current.set(record)
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            record["status"] = record["status"] or 500
            raise
        finally:
            record["duration_ms"] = (time.perf_counter() - start) * 1000.0
            _current.reset(token)
            with _lock:
                if len(_records) < MAX_RECORDS:
                    _records.append(_serializable(record))

    async def _control(self, scope, send):
        path = scope["path"][len(CONTROL_PREFIX):]
        if path == "/health":
            body = {"ok": True, "pid": os.getpid(), **self.status}
        elif path == "/records":
            with _lock:
                body = {"records": list(_records)}
                _records.clear()
        else:
            body = {"error": "unknown control path"}
        payload = json.dumps(body).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


def load_app(spec: str):
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    app = module
    for part in (attr or "app").split("."):
        app = getattr(app, part)
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ScaleTrace runtime bootstrap (executes the target app).")
    parser.add_argument("--app", required=True, help="module:attribute of the ASGI app, e.g. app.main:app")
    parser.add_argument("--root", required=True, help="Target project root (added to sys.path).")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--trace-calls", action="store_true")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    os.chdir(root)
    sys.path.insert(0, root)
    locator = Locator(root)
    status = {"sqlalchemy": install_sql_hooks(locator), "trace_calls": bool(args.trace_calls)}
    app = load_app(args.app)

    if args.trace_calls:
        profiler = make_profiler(locator)
        if hasattr(threading, "setprofile_all_threads"):
            threading.setprofile_all_threads(profiler)
        else:
            threading.setprofile(profiler)
        sys.setprofile(profiler)

    import uvicorn

    uvicorn.run(InstrumentedApp(app, status), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
