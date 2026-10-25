"""Controlled runtime testing: start the target under the bootstrap, drive a workload, collect metrics.

Executes target code. Callers must obtain explicit consent (the CLI requires --allow-execute).
"""

from __future__ import annotations

import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from scaletrace import __version__
from scaletrace.runtime.workload import RequestSpec, WorkloadSpec, render_path, substitute
from scaletrace.schemas.runtime import EndpointRuntime, Measurement, ObservedEdge, RuntimeReport, StatementCount

BOOTSTRAP = Path(__file__).with_name("bootstrap.py")
CONTROL = "/__scaletrace__"
TOP_STATEMENTS = 5
LOG_TAIL_LINES = 20

RUNTIME_LIMITATIONS = [
    "Measurements come from one local process under a synthetic workload; they show behaviour under "
    "this workload only, not production behaviour.",
    "CPU and memory are process-level (psutil): they cannot be attributed to individual endpoints "
    "when requests overlap, and memory includes interpreter and library overhead.",
    "With call tracing enabled, sys.setprofile adds overhead to every Python call, so absolute "
    "latencies are inflated; growth with input size remains comparable.",
    "Query counts and attribution cover SQLAlchemy engines only; other drivers are not instrumented.",
]


class RunnerError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class _ResourceSampler(threading.Thread):
    """Samples RSS and CPU% of the target process while a size is being measured."""

    def __init__(self, pid: int, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self._halt = threading.Event()
        self.rss: list[float] = []
        self.cpu: list[float] = []
        try:
            import psutil

            self.process = psutil.Process(pid)
            self.process.cpu_percent(None)
        except Exception:
            self.process = None

    def run(self) -> None:
        while self.process is not None and not self._halt.is_set():
            try:
                self.rss.append(self.process.memory_info().rss / 1_048_576)
                self.cpu.append(self.process.cpu_percent(None))
            except Exception:
                return
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)

    def baseline_rss(self) -> float | None:
        if self.process is None:
            return None
        try:
            return self.process.memory_info().rss / 1_048_576
        except Exception:
            return None


class RuntimeRunner:
    def __init__(self, repo_root: str | Path, spec: WorkloadSpec, log_dir: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.spec = spec
        self.workdir = (self.repo_root / spec.working_dir).resolve()
        if self.repo_root not in self.workdir.parents and self.workdir != self.repo_root:
            raise RunnerError("working_dir must stay inside the repository")
        self.log_path = Path(log_dir) / "runtime_server.log"
        self.process: subprocess.Popen | None = None
        self.target_pid: int | None = None  # the interpreter serving requests (venv launchers spawn a child)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

    # --- process lifecycle ----------------------------------------------------------------------

    def _start(self, client) -> dict:
        python = self.spec.python or sys.executable
        cmd = [python, "-I", str(BOOTSTRAP), "--app", self.spec.app, "--root", str(self.workdir), "--port", str(self.port)]
        if self.spec.trace_calls:
            cmd.append("--trace-calls")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.process = subprocess.Popen(cmd, cwd=self.workdir, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.spec.startup_timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RunnerError(f"target exited during startup (code {self.process.returncode}):\n{self._log_tail()}")
            try:
                response = client.get(f"{self.base_url}{CONTROL}/health", timeout=1.0)
                if response.status_code == 200:
                    return response.json()
            except Exception:
                pass
            time.sleep(0.2)
        raise RunnerError(f"target did not become ready within {self.spec.startup_timeout_s}s:\n{self._log_tail()}")

    def _stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if getattr(self, "_log", None) is not None:
            self._log.close()

    def _log_tail(self) -> str:
        try:
            self._log.flush()
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-LOG_TAIL_LINES:])
        except OSError:
            return ""

    # --- requests -------------------------------------------------------------------------------

    def _send(self, client, request: RequestSpec, size: int) -> tuple[int | None, float]:
        start = time.perf_counter()
        try:
            response = client.request(
                request.method,
                self.base_url + render_path(request, size),
                params=substitute(request.params, size),
                json=substitute(request.json_body, size),
                timeout=self.spec.request_timeout_s,
            )
            return response.status_code, (time.perf_counter() - start) * 1000.0
        except Exception:
            return None, (time.perf_counter() - start) * 1000.0

    def _records(self, client) -> list[dict]:
        return client.get(f"{self.base_url}{CONTROL}/records", timeout=30).json()["records"]

    def _measure_size(self, client, size: int, deadline: float) -> dict[str, Measurement]:
        for setup in self.spec.setup:
            status, _ = self._send(client, setup, size)
            if status is None or status >= 400:
                raise RunnerError(f"setup request {setup.display_name} failed at size {size} (status {status})")
        for _ in range(self.spec.warmup):
            for request in self.spec.requests:
                self._send(client, request, size)
        self._records(client)  # discard setup and warm-up records

        sampler = _ResourceSampler(self.target_pid)
        baseline_rss = sampler.baseline_rss()
        sampler.start()
        jobs = [r for _ in range(self.spec.repetitions) for r in self.spec.requests]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self.spec.concurrency) as pool:
            results = list(pool.map(lambda r: (r, *self._send(client, r, size)), jobs))
        wall = time.perf_counter() - started
        sampler.stop()
        if time.monotonic() > deadline:
            raise RunnerError(f"total timeout of {self.spec.total_timeout_s}s exceeded")

        records = self._records(client)
        by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for record in records:
            by_key[(record["method"], record["path"])].append(record)

        measurements: dict[str, Measurement] = {}
        for request in self.spec.requests:
            mine = [res for res in results if res[0] is request]
            server = by_key.get((request.method, render_path(request, size)), [])
            measurements[request.display_name] = self._aggregate(size, mine, server, wall, sampler, baseline_rss)
        return measurements

    def _aggregate(self, size, client_results, server_records, wall, sampler, baseline_rss) -> Measurement:
        statuses = Counter(str(status) if status is not None else "error" for _, status, _ in client_results)
        errors = sum(1 for _, status, _ in client_results if status is None or status >= 500)
        latencies = [r["duration_ms"] for r in server_records] or [ms for _, _, ms in client_results]
        n = len(server_records)

        def per_request(key: str) -> dict[str, float]:
            total: Counter = Counter()
            for record in server_records:
                total.update(record[key])
            return {k: v / n for k, v in total.items()} if n else {}

        edges: Counter = Counter()
        statements: Counter = Counter()
        for record in server_records:
            for caller, callee, count in record["calls"]:
                edges[(caller, callee)] += count
            statements.update(record["statements"])
        return Measurement(
            size=size,
            requests=len(client_results),
            errors=errors,
            status_codes=dict(statuses),
            latency_ms_mean=statistics.fmean(latencies) if latencies else None,
            latency_ms_p50=_percentile(latencies, 0.5),
            latency_ms_p95=_percentile(latencies, 0.95),
            throughput_rps=len(client_results) / wall if wall > 0 else None,
            queries_per_request=(sum(r["queries"] for r in server_records) / n) if n else None,
            query_time_ms_per_request=(sum(r["query_time_ms"] for r in server_records) / n) if n else None,
            rss_mb_peak=max(sampler.rss) if sampler.rss else None,
            rss_mb_delta=(max(sampler.rss) - baseline_rss) if sampler.rss and baseline_rss is not None else None,
            cpu_percent_mean=statistics.fmean(sampler.cpu) if sampler.cpu else None,
            function_calls_per_request=per_request("functions"),
            queries_by_function_per_request=per_request("queries_by_function"),
            call_edges=[ObservedEdge(caller=c, callee=e, calls_per_request=v / n) for (c, e), v in sorted(edges.items())] if n else [],
            top_statements=[StatementCount(statement=s, per_request=v / n) for s, v in statements.most_common(TOP_STATEMENTS)] if n else [],
        )

    # --- entry point ----------------------------------------------------------------------------

    def run(self) -> RuntimeReport:
        import httpx

        started = time.monotonic()
        deadline = started + self.spec.total_timeout_s
        report = RuntimeReport(
            scaletrace_version=__version__,
            repository=self.repo_root.name,
            app=self.spec.app,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status="completed",
            sizes=list(self.spec.sizes),
            limitations=list(RUNTIME_LIMITATIONS),
        )
        series: dict[str, EndpointRuntime] = {
            r.display_name: EndpointRuntime(endpoint=r.label, request_name=r.display_name) for r in self.spec.requests
        }
        try:
            with httpx.Client() as client:
                health = self._start(client)
                self.target_pid = int(health.get("pid") or self.process.pid)
                report.instrumentation = {
                    "sqlalchemy_queries": bool(health.get("sqlalchemy")),
                    "call_tracing": bool(health.get("trace_calls")),
                    "process_resources": _ResourceSampler(self.target_pid).process is not None,
                }
                for size in self.spec.sizes:
                    for name, measurement in self._measure_size(client, size, deadline).items():
                        series[name].measurements.append(measurement)
        except RunnerError as exc:
            report.status = "failed"
            report.error = str(exc)
        finally:
            self._stop()
        report.endpoints = list(series.values())
        report.duration_s = round(time.monotonic() - started, 2)
        if not report.instrumentation.get("sqlalchemy_queries"):
            report.unavailable_metrics.append("database query counts (SQLAlchemy not detected in the target)")
        if not report.instrumentation.get("call_tracing"):
            report.unavailable_metrics.append("observed call edges and per-function call counts (call tracing off)")
        if not report.instrumentation.get("process_resources"):
            report.unavailable_metrics.append("process CPU/memory (psutil unavailable)")
        report.unavailable_metrics.append("external service call counts (HTTP clients are not instrumented)")
        return report


def run_workload(repo_root: str | Path, spec: WorkloadSpec, log_dir: str | Path) -> RuntimeReport:
    return RuntimeRunner(repo_root, spec, log_dir).run()
