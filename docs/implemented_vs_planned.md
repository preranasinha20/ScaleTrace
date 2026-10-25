# Implemented vs planned

Last updated: 2026-10-25.

## Implemented

| Area | Feature |
|---|---|
| Static analysis | Ingestion (skips venvs, hidden dirs, caches, tests, symlinks, large files); AST parsing with graceful skips; FastAPI routes incl. `APIRouter(prefix=...)` |
| | Project index: imports (absolute/relative), call resolution, `Depends()` edges, per-function facts with loop context |
| | DB-handle inference (annotation / dependency / factory / name, basis recorded); HTTP-client inference |
| Rules | **DB-001** (direct + indirect via call graph), **DB-003**, **CMP-001**, **MEM-001**, **MEM-002** (module/class collections), **EXT-001** (direct + indirect) |
| Knowledge graph | Endpoints, functions, database, external services; routes_to / depends_on / calls / queries / calls_external; findings attached; risk paths; JSON; interactive HTML (vis-network) |
| Runtime | `scaletrace run --allow-execute`; workload spec with `{size}` sweeps; bootstrap with SQLAlchemy query count/time/statements, per-function query attribution, observed call edges, latency, CPU/RSS; timeouts; 127.0.0.1 only |
| Diagnosis | Rule-specific correlation (correlated / not_confirmed / unchanged + reason); symptom detection (query, latency, memory growth, errors); runtime merge into the graph; backtracking from endpoint or database with `Risk(v)`; step-by-step explanation; probable cause; unexplained observations |
| Reports | Text, JSON (+ JSON Schema), Markdown, HTML; sample finding |
| Evaluation | Ground truth matching; precision / recall / F1 per rule, category, overall; CSV export (`scaletrace evaluate`) |
| Deep learning | Synthetic labelled app generator (6 defect kinds, 3 loop depths, decoys, 2 styles); runtime collection; feature groups; directed GraphSAGE GNN (PyTorch); listwise ranking loss; runtime-dropout; checkpoints; GNNExplainer-style explanations; GNN as ranker in `diagnose --model`; comparative experiment (`scaletrace ml experiment`) |
| Benchmark (Phase 1) | `scaletrace benchmark init/validate/split/lock/summary`; manifest schema with per-defect verification; grouped stratified dev/test split; test-set lock (hashes); guard keeping benchmark apps out of training/selection; `evaluate --manifest [--final]`; `ml evaluate --final` with `final_runs.log` |
| ML validity (Phase 1) | Generator v4 (shared fragments, hard decoys over a fixed-size table, depth 0-3, class-based services, 1.x/2.0 query styles, neutral names); runtime label verification (`ml verify-labels`); shortcut-check suite (`ml audit-data`); node-only MLP baseline; leave-one-kind-out split; per-seed mean ± sd; app-level bootstrap CIs; pre-declared validation gate (`ml experiment`) |
| UI | Streamlit app: repository picker (restricted root), runtime consent, findings, diagnoses, embedded graph, exports |
| Examples | `orders_app_defective` / `orders_app_fixed`, multi-module `shop_app` (one injected risk per implemented rule), all with ground truth, root-cause labels, and workloads |

## Partially implemented

* Pretrained code-Transformer embeddings (`--embedder codebert`): implemented, not evaluated here (needs
  `transformers` and a model download).
* Memory evidence: process-level RSS only; no heap/object tracking.

## Phase 2 (not started; explicit tasks)

* Integrate the verified team benchmark (manifests, split, lock) and run the final evaluation once.
* Explainer fix (degenerate GNNExplainer masks).
* ZIP upload with a per-session workspace.
* Safe runtime isolation (container with CPU/memory/network limits) before any uploaded or public code
  is executed. Until then, runtime testing is only for trusted local code.

## Planned / deferred

* DB-002 (repeated equivalent query) and CMP-002 (loop-invariant computation) -- need data-flow analysis.
* Extension rules: ASY-001, REL-001/002, CON-001/002, EXT-002, RES-001/002.
* `include_router(prefix=...)` across modules; deeper type inference for call resolution.
* External HTTP call counting at runtime (patching httpx/requests in the bootstrap).
* Repeated-request memory test for MEM-002.
* Evaluation on the team's benchmark codebases (03_good_codebases / 04_bad_codebases) once they exist.
* Locust, OpenTelemetry, container isolation.
