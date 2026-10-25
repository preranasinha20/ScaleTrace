"""Runtime verification of generated labels (generated apps only; never benchmark apps).

A generated root-cause label is *runtime-verified* when its endpoint's metric grows with input size
under the cached runtime report:

* query_growth   -- queries per request: growth exponent >= 0.5 and an increase of >= 5 queries;
* latency_growth -- mean latency minus SQL time: growth exponent >= 0.5 and >= 10 ms at the largest size.

Hard decoys are checked to stay constant: the queries attributed to the decoy function and its call
count must have a growth exponent < 0.3. A label whose decoys grow is kept but flagged.

Verification results are written back into the generator's ``scaletrace_rootcause.json`` so that
`load_bundle(..., require_verified=True)` uses only verified labels.
"""

from __future__ import annotations

import json
from pathlib import Path

from scaletrace.benchmark.guard import TestSetAccessError, classify
from scaletrace.diagnosis.correlate import match_runtime_endpoint
from scaletrace.diagnosis.symptoms import compute_latency_series
from scaletrace.ml.dataset import RUNTIME_CACHE
from scaletrace.ml.generator import ROOTCAUSE_FILE
from scaletrace.runtime.analysis import growth_exponent
from scaletrace.schemas.runtime import RuntimeReport

QUERY_EXPONENT = 0.5
QUERY_INCREASE = 5.0
LATENCY_EXPONENT = 0.5
LATENCY_MIN_MS = 10.0
DECOY_MAX_EXPONENT = 0.3


def _endpoint_check(kind: str, endpoint) -> tuple[bool, dict]:
    measurements = endpoint.by_size()
    sizes = [m.size for m in measurements]
    if kind == "query_growth":
        values = [m.queries_per_request for m in measurements]
        exponent = growth_exponent(sizes, values, offset=1.0)
        known = [v for v in values if v is not None]
        ok = exponent is not None and exponent >= QUERY_EXPONENT and known and max(known) - min(known) >= QUERY_INCREASE
    else:
        sizes, values = compute_latency_series(endpoint)
        exponent = growth_exponent(sizes, values)
        known = [v for v in values if v is not None]
        ok = exponent is not None and exponent >= LATENCY_EXPONENT and known and max(known) >= LATENCY_MIN_MS
    return bool(ok), {"metric": kind, "sizes": sizes, "values": values, "exponent": exponent}


def _decoy_constant(func_id: str, endpoint) -> bool:
    measurements = endpoint.by_size()
    sizes = [m.size for m in measurements]
    queries = [m.queries_by_function_per_request.get(func_id, 0.0) for m in measurements]
    calls = [m.function_calls_per_request.get(func_id, 0.0) for m in measurements]
    for series in (queries, calls):
        exponent = growth_exponent(sizes, series, offset=1.0)
        if exponent is not None and exponent >= DECOY_MAX_EXPONENT:
            return False
    return True


def _edge_growth(endpoint, caller: str, callee: str) -> float | None:
    measurements = endpoint.by_size()
    sizes = [m.size for m in measurements]
    calls = [next((e.calls_per_request for e in m.call_edges if e.caller == caller and e.callee == callee), 0.0) for m in measurements]
    return growth_exponent(sizes, calls, offset=1.0) if any(calls) else None


def verify_app(app_dir: str | Path) -> dict:
    """Annotate one generated app's labels with runtime verification; returns counts."""
    app_dir = Path(app_dir)
    if classify(app_dir)[0]:
        raise TestSetAccessError(f"{app_dir} is a benchmark application; label verification only runs on generated apps")
    labels_path = app_dir / ROOTCAUSE_FILE
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    runtime_path = app_dir / RUNTIME_CACHE
    runtime = RuntimeReport.model_validate_json(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else None
    counts = {"verified": 0, "rejected": 0, "no_runtime": 0, "decoys_growing": 0}
    for symptom in labels.get("symptoms", []):
        endpoint = match_runtime_endpoint(symptom["endpoint"], runtime) if runtime and runtime.status == "completed" else None
        if endpoint is None or len(endpoint.measurements) < 2:
            symptom["verification"] = {"runtime": "unverified", "reason": "no runtime measurements"}
            counts["no_runtime"] += 1
            continue
        ok, evidence = _endpoint_check(symptom["kind"], endpoint)
        decoys = {d: ("constant" if _decoy_constant(d, endpoint) else "grows") for d in symptom.get("decoy_functions", [])}
        counts["decoys_growing"] += sum(1 for v in decoys.values() if v == "grows")
        symptom["verification"] = {"runtime": "verified" if ok else "rejected", **evidence, "decoys": decoys}
        counts["verified" if ok else "rejected"] += 1
        if symptom.get("mode") == "shared_helper":
            # Graph-required only if the evidence really sits on the edges: root->helper calls grow,
            # twin->helper calls stay constant. Otherwise the case is not counted as graph-required.
            helper = symptom["shared_helper"]
            root_growth = _edge_growth(endpoint, symptom["root_cause_functions"][0], helper)
            twin_growth = _edge_growth(endpoint, symptom["twin_decoy"], helper)
            graph_ok = (root_growth is not None and root_growth >= QUERY_EXPONENT
                        and (twin_growth is None or twin_growth < DECOY_MAX_EXPONENT))
            symptom["verification"]["graph_evidence"] = {
                "root_to_helper_growth": root_growth, "twin_to_helper_growth": twin_growth,
                "status": "verified" if graph_ok else "failed",
            }
            symptom["graph_required"] = bool(graph_ok)
            counts["graph_required"] = counts.get("graph_required", 0) + int(graph_ok)
    labels_path.write_text(json.dumps(labels, indent=2), encoding="utf-8")
    return counts
