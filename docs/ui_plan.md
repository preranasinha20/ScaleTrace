# Custom UI plan (investigation, 2026-10-26)

Status: **investigation only**. The Streamlit UI stays the demo until a replacement is proven.

## What exists today

- `scaletrace/app/streamlit_app.py` calls the engine directly as Python functions:
  `run_static_analysis` → `diagnose_repository` (optional GNN via `ml.inference.load_ranker`) →
  `graph_to_json` / `render_graph_html` (vis-network 9.1.9) / `render_diagnosis_markdown`.
- **No HTTP API exists.** All outputs are already JSON-serialisable (pydantic models, `graph_to_json`).
- Headless end-to-end check: `tests/integration/test_ui_smoke.py`.

## Recommended architecture (simplest that works)

```
browser (HTML + CSS + TypeScript, built with Vite)
   │  fetch JSON
   ▼
scaletrace serve  (small FastAPI app, 127.0.0.1 only, same process serves the built static files)
   │  direct Python calls (no engine changes)
   ▼
existing engine: pipeline / diagnosis / graph / reports / ml.inference
```

Endpoints (read-only, allow-listed projects only, nothing executed):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects` | demo projects under `examples/` (same allow-list rules as the Streamlit UI) |
| GET | `/api/models` | v5 checkpoints (same filter as the Streamlit UI) |
| POST | `/api/analyze` `{project, model?, endpoint?}` | findings, diagnoses with ranked candidates, `graph_to_json` |
| GET | `/api/report/{run_id}.md` / `.json` | report downloads |

Why: one language for the engine, one process to start, no database, no auth (local demo). The front
end is a static bundle, so it can later be hosted separately without changing the API.

## Work needed

| Piece | Work | Estimate |
|---|---|---|
| API (`scaletrace/api/`, `scaletrace serve`) | 4 endpoints wrapping existing functions, run cache keyed by project+model, tests with `httpx` | 0.5 day |
| Findings table | sortable/filterable table (rule, severity, status, file:line, endpoint); click → highlight node | 0.5 day |
| Ranked root-cause panel | per diagnosis: symptom, evidence steps (labelled static / runtime / graph / inference), candidate list with risk and GNN score, "probable cause, not proof" wording | 0.5 day |
| Interactive knowledge graph | reuse vis-network with the JSON we already produce (left-to-right layers, risk paths highlighted, click node → findings); Cytoscape.js is an alternative with better layered layouts | 0.5–1 day |
| Report downloads | links to the API's `.md` / `.json` | 1 hour |
| Polish / layout | three-pane layout, dark/light theme | 0.5 day |

Total ≈ 2.5–3.5 days: realistic **after** Tuesday, not before. ZIP upload is a later, separate step that
needs safe extraction (size and path checks) and stays static-only until a sandbox exists.

## Design ideas from developer tools

- **CodeScene hotspots**: risk shown as colour on a map; ranked "refactoring targets" list next to it, so
  the user sees *where* and *what first*. → colour graph nodes by risk; show the ranked list beside it.
  <https://codescene.io/docs/guides/technical/hotspots.html>,
  <https://codescene.com/resources/use-cases/refactoring-targets>
- **Sentry N+1 issue page**: evidence laid out as transaction → parent span → repeating span → the
  repeated query. → our diagnosis panel already has the same chain (endpoint → function → loop → SQL);
  present it as that vertical "evidence trail".
  <https://docs.sentry.io/product/issues/issue-details/performance-issues/n-one-queries>
- **Grafana service graph**: node graph with per-node numbers and colour for health, a metrics table
  beside it, and click-for-context-menu navigation. → node badges (queries/request), table + graph side by side.
  <https://grafana.com/docs/grafana/v12.4/datasources/tempo/service-graph/>
- **Cytoscape.js demos**: layered/hierarchical layouts and styling for large graphs.
  <https://js.cytoscape.org/>
