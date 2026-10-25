# ScaleTrace — Project Handoff Summary

_Context for another AI assistant. Last updated: 2026-10-25 (after the full build)._

## 1. What ScaleTrace is

A 7-week research prototype that analyzes **Python/FastAPI codebases** for **scalability risks**,
builds a **knowledge graph** (endpoint -> function -> database / external service, with findings
attached), optionally **runs the app** under a controlled workload, **correlates** static and runtime
evidence, and **backtracks** from a symptom (e.g. "queries per request grow with data size") to a
probable source-code cause. A **graph neural network** can replace the hand-written ranking step.

Research question: does combining static and runtime evidence give better, more explainable
diagnoses than static analysis alone? Literature warning: PRAXIS, ScaleLens and ORM-analysis papers
already combine static and runtime evidence, so novelty must come from the specific evaluation.

## 2. Source documents
Charter (`01_scope/`, primary scope), rulebook JSON + dictionary (`02_initial_rules/`, 16 rules,
8 core), literature review xlsx (owner's folder `C:\Users\omban\Desktop\ScaleTrace\`), Figure 4.4
(backtracking engine with `Risk(v) = w_s S + w_d D + w_p P`).

## 3. Hard rules
Static means "could happen"; runtime means "happened under this workload". Statuses: `static_candidate`,
`runtime_observed`, `correlated_finding`, `not_confirmed`. No confidence percentages. "Probable cause"
only. Static analysis never executes code; runtime needs explicit consent (`--allow-execute`).
**The AI must not commit or push** (owner does git). **Present plans before building new phases.**

## 4. Decisions
See `docs/decisions.md` (D0-D15). Key ones: the knowledge graph is the central output; the charter's
3 categories; the rulebook is read-only (tuning in `DetectorConfig`); MEM-002 only flags
module/class-level collections; **deep learning is core** (GNN ranker, CPU, synthetic training data,
team benchmarks used for testing only); root-cause label = loop owner; heuristic is the default
ranker, the GNN is opt-in.

## 5. What is implemented (repo `C:\Users\omban\Desktop\Projects\ScaleTrace`)
Python 3.13, `.venv` (pydantic, networkx, pytest, httpx, psutil, fastapi, sqlalchemy, uvicorn,
requests, torch CPU, numpy, streamlit).

| Module | Content |
|---|---|
| `analyzer/` | ingestion, AST, scopes/routes, loop context, DB-handle and HTTP-client inference, **project index with call resolution** |
| `rules/detectors/` | **DB-001** (direct + indirect), **DB-003**, **CMP-001**, **MEM-001**, **MEM-002**, **EXT-001** |
| `graph/` | knowledge graph, runtime merge (observed edges), risk paths, JSON, interactive HTML (vis-network) |
| `runtime/` | workload spec (`scaletrace_workload.json`, `{size}` sweeps), standalone bootstrap (SQLAlchemy query count/time/statements, **per-function query attribution**, **observed call edges**, latency, CPU/RSS), runner |
| `diagnosis/` | correlation (status + reason), symptoms, backtracking with `Risk(v)`, step-by-step explanation |
| `reports/` | text, JSON, Markdown, HTML |
| `evaluation/` | ground truth, precision/recall/F1 per rule/category, CSV |
| `ml/` | synthetic app generator, features (type/structure/rules/symptom/runtime/code), hashed + optional CodeBERT embeddings, directed GraphSAGE GNN (plain PyTorch), listwise loss, GNNExplainer-style explanations, inference in `diagnose --model`, `ml experiment` |
| `app/streamlit_app.py` | UI (restricted root, consent checkbox, graph, diagnoses, exports) |

CLI: `scaletrace analyze | graph | run | diagnose | evaluate | rules | schema | ml {generate,
collect-runtime, train, evaluate, experiment}`.

Examples: `orders_app_defective` / `orders_app_fixed`, multi-module `shop_app` (one risk per rule),
each with ground truth, root-cause labels and a workload.

Verified results:
- Static: all injected risks found in the 3 examples, 0 false positives (`scaletrace evaluate`).
- Runtime: the orders demo shows 11 -> 101 -> 1001 queries; DB-001 `correlated_finding`, MEM-001
  `not_confirmed`.
- Shop: DB-001 (indirect) and DB-003 correlated.
- On a generated app, a defect the rules cannot see (`db_hidden`) is honestly reported "unexplained".
- Tests: 146 passed after Phase 1 (109 earlier tests unchanged + 23 benchmark + 14 ML-validity).

## 6. Phase 1 (2026-10-25): benchmark tooling + research validity

**Benchmark (Workstream A, done).** Per-app `scaletrace_benchmark.json` manifest:
- per-defect location, rule, endpoint, root cause, path, and static/runtime verification
- `scaletrace benchmark init|validate|split|lock|summary`
- grouped, stratified dev/test split, and a hash lock of the test split
- a guard (`scaletrace/benchmark/guard.py`):
  - *training:* refuses every team/public app
  - *diagnostic:* refuses test apps
  - *final:* requires a lock and logs to `final_runs.log`
- `scaletrace evaluate --manifest [--final]`: only fully verified defects count

The examples are migrated, with identical metrics. Guide: `docs/benchmark.md`.
Agreed dev policy: diagnostics and realism only, never model selection (`docs/dev_log.md`).

**ML validity (Workstream B, done; result negative).**
- Generator v4: shared fragments; hard decoys over a fixed 3-row table (statically identical to roots);
  depth 0-3; class-based services; mixed query styles; neutral names.
- Runtime label verification (`ml verify-labels`).
- Shortcut-check suite (`ml audit-data`).
- Node-only MLP baseline.
- Leave-one-kind-out split.
- Per-seed statistics and app-level bootstrap CIs.
- Pre-declared gate (`ml experiment`).

The suite correctly FAILS the old v3 data (node-only MLP 1.000, fingerprint 0.70).

v4 verdict: **GNN NOT VALIDATED.**
- The node-only MLP reaches Top-1 0.955 (shortcut check FAIL), and the GNN is within noise of it.
- The GNN does beat every non-learned baseline by a wide margin (MRR 0.984 vs best heuristic 0.659).
- Runtime evidence helps on this synthetic data (gnn_static 0.84 vs gnn_combined 0.98), partly by
  construction of the hard decoys.

Details and the proposal for testing the graph (shared helpers plus per-edge observed call counts as
edge features) are in `docs/ml.md`.

## 7. Phase 2 (2026-10-25)

- **Dataset:** github.com/preranasinha20/ScaleTrace-Dataset @ a159548 (2 CSVs). Status and
  team to-dos: `docs/dataset_status.md`.
  - 52 candidates imported (`benchmark import-register`).
  - 34 of 35 non-rejected repos pinned and fetched for static analysis (`benchmark fetch`, never
    executed). `seva-pulse` is unavailable.
  - Manifests drafted with no defects; screening report written (`benchmark screen`).
  - Validation is currently INVALID: 9 bad apps have no labels, 125 good-app rule hits await review.
  - No commit hashes or verified labels were in the CSVs. The bad set covers only MEM-001/MEM-002 claims.
- **Final evaluation:** `benchmark evaluate-final` (locked test split, logged; `--dry-run` on dev).
  Static-only, tested on a mock locked benchmark.
- **ML:** generator v5 (shared-helper twins, so the graph is needed), edge-conditioned GNN, and extra
  baselines `mlp_hop` and `heuristic_combined_edges`.
  - The pre-declared gate PASSED on synthetic held-out data.
  - However, `mlp_hop` (hand-engineered one-hop MLP) ties the GNN, and the data was redesigned after
    earlier failures. See `docs/ml.md`.
- **Tests:** 161 passed.

## 8. Next steps
1. Team: label and verify defects in the drafted manifests; build controlled defective copies covering
   DB-001/DB-003/CMP-001; exclude unsuitable repos.
2. Then: validate, split, lock, `evaluate-final --dry-run`, then one `evaluate-final` with frozen checkpoints.
3. Sandbox for runtime on untrusted code (needed before any runtime evaluation on real repos).
4. Explainer fix. ZIP upload and UI are deferred until asked.
