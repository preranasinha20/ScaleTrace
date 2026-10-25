# ScaleTrace architecture

ScaleTrace is a modular pipeline. Each stage has one responsibility and communicates through the
pydantic models in `scaletrace/schemas/` (findings, runtime measurements, diagnoses).

```
repository path
   |
[analyzer/ingestion]   list .py files; skip venvs, hidden dirs, caches, tests; never import target code
[analyzer/module]      parse with `ast`; scopes (functions, methods), FastAPI routes (incl. APIRouter prefix)
[analyzer/project]     project index: imports, call resolution, per-function facts
                       (calls, DB accesses, external calls, each with loop context)
   |
[rules/detectors/*]    DB-001 (direct + indirect), DB-003, CMP-001, MEM-001, MEM-002, EXT-001
   |                   -> static findings (status static_candidate); metadata from the rulebook JSON
   v
[graph]                KNOWLEDGE GRAPH: endpoint -> function -> database / external service,
   |                   findings attached to function nodes; risk paths; JSON + interactive HTML
   |
[runtime]  (opt-in)    target started in a subprocess under runtime/bootstrap.py (127.0.0.1 only):
   |                   per-request SQL count/time/statement, SQL attributed to the project function
   |                   that issued it, observed call edges (sys.setprofile), latency, CPU/RSS (psutil);
   |                   driven by a workload spec at several input sizes
   v
[diagnosis/correlate]  rule-specific runtime metric grows with input size?  -> correlated_finding /
   |                   not_confirmed / unchanged (with the reason recorded)
[graph.merge_runtime]  mark observed edges; add observed edges the static graph missed
[diagnosis/symptoms]   query / latency / memory growth, errors -> symptoms per endpoint
[diagnosis/backtrack]  from symptom endpoint (or the database node) to candidate functions,
   |                   ranked by Risk(v) = w_s S + w_d D + w_p P  (or by the learned GNN),
   |                   diagnostic path + step-by-step explanation + probable cause
   v
[reports]              text, JSON, Markdown, HTML; [app] Streamlit UI

[ml] (deep learning)   synthetic labelled apps -> features over the knowledge graph -> GNN
                       root-cause ranker (+ GNNExplainer-style explanations) -> plugs into backtrack
```

## Package layout

| Path | Responsibility |
|---|---|
| `scaletrace/schemas/` | `Finding`, `AnalysisReport`, `RuntimeReport`, `DiagnosisReport` |
| `scaletrace/rules/` | rulebook loader/validator, `DetectorConfig`, detectors (one module per rule) |
| `scaletrace/analyzer/` | ingestion, parsing, scopes/routes, loops, DB-handle and HTTP-client inference, project index |
| `scaletrace/graph/` | knowledge graph build, runtime merge, risk paths, JSON, HTML viewer |
| `scaletrace/runtime/` | workload spec, bootstrap (runs inside target), runner, growth analysis |
| `scaletrace/diagnosis/` | correlation, symptoms, backtracking, end-to-end engine, `DiagnosisConfig` |
| `scaletrace/reports/` | text / JSON / Markdown / HTML reports |
| `scaletrace/evaluation/` | ground-truth format, precision / recall / F1 per rule and category |
| `scaletrace/ml/` | generator, features, embeddings, GNN model, training, evaluation, explainer, inference |
| `scaletrace/app/` | Streamlit UI |
| `scaletrace/cli.py`, `ml/cli.py` | command-line interface |

## Knowledge graph

| Node | Id | Key attributes |
|---|---|---|
| endpoint | `endpoint:GET /orders` | method, path, handler |
| function | `function:app.main:list_orders` | file, line, loops, DB/external accesses (in loops), findings, rules, max severity, observed counts |
| resource | `resource:database`, `resource:external:<host>` | resource type |

| Edge (`kind`) | From -> to | Attributes |
|---|---|---|
| `routes_to` | endpoint -> handler | |
| `depends_on` | function -> FastAPI dependency | |
| `calls` | function -> function | lines, `in_loop`, `static`, `observed`, observed calls/request |
| `queries` | function -> database | `in_loop`, observed queries/request |
| `calls_external` | function -> external service | `in_loop` |

A *risk path* is the shortest path from an endpoint to a function carrying findings (observed edges
preferred). It answers "where can a scaling problem occur, and which endpoints does it affect"
without running anything.

## Design choices

* **The rulebook is read-only input**; detector vocabulary lives in `DetectorConfig` (`--config`).
* **Evidence records its own strength**: DB-handle inference basis, direct vs indirect access,
  loop variables used, call chains, which runtime metric supported or failed to support a finding.
* **No numeric confidence** (rulebook policy). Severity is impact; status and evidence carry certainty.
* **Static analysis never executes code.** Runtime testing is a separate, explicit, consented step.
* **The diagnosis engine is deterministic by default.** The GNN is an optional ranker that plugs into
  the same backtracking step; its output is labelled "learned" and comes with an explanation.
* **No PyTorch Geometric**: the GNN layer is ~30 lines of plain PyTorch, avoiding a heavy dependency.
