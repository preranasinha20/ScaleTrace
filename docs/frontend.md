# ScaleTrace web frontend

React 18 + TypeScript + Vite in `frontend/`, backed by a FastAPI adapter in `scaletrace/api/server.py`.
The adapter calls the same functions as the Streamlit UI (`run_static_analysis`, `diagnose_repository`,
`risk_paths`, the report renderers). No analysis logic is duplicated in TypeScript.

## Prerequisites

- Python 3.10+ with the repository `.venv` (see the README Setup section). Torch is needed only for the learned rankers.
- Node.js 18+ and npm (tested with Node 24.11, npm 11.6).

## Install

```bash
.venv\Scripts\python -m pip install -e ".[api]"
npm --prefix frontend install
```

## Run

**Single process (production build served by the API):**

```bash
npm --prefix frontend run build
.venv\Scripts\python -m scaletrace.api
```

Open <http://127.0.0.1:8000>. If `frontend/dist` exists, the API serves it, and `/privacy` and `/terms` work on refresh.

**Development (hot reload), two terminals:**

```bash
.venv\Scripts\python -m scaletrace.api
```

```bash
npm --prefix frontend run dev
```

Open <http://127.0.0.1:5173>. Vite proxies `/api` to the backend, so the dev setup needs no CORS configuration.
In the Claude desktop app, the launch configurations `scaletrace-api` and `scaletrace-web-dev` do the same.

API docs (OpenAPI) are at <http://127.0.0.1:8000/api/docs>.

## Configuration

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `SCALETRACE_ALLOWED_ROOT` | API | current directory | Only folders under it can be analysed. `benchmark_data`, `.venv`, `node_modules`, `scaletrace_output`, `frontend` and hidden folders are never listed. |
| `SCALETRACE_CORS_ORIGINS` | API | `http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173` | Comma-separated origins allowed to call the API from another origin. |
| `SCALETRACE_FRONTEND_DIST` | API | `frontend/dist` | Built frontend to serve at `/`. |
| `VITE_API_BASE_URL` | frontend build | empty (same origin) | Set only when the frontend is hosted on a different origin from the API. |
| `SCALETRACE_API_PROXY` | Vite dev/preview | `http://127.0.0.1:8000` | Where `/api` is proxied. |
| `SCALETRACE_UPLOAD_MAX_BYTES` | API | `20971520` (20 MB) | Largest accepted ZIP (checked while the body streams in). |
| `SCALETRACE_UPLOAD_MAX_EXTRACTED_BYTES` | API | `104857600` (100 MB) | Total size of extracted source files. |
| `SCALETRACE_UPLOAD_MAX_FILE_BYTES` | API | `2000000` | Largest single extracted file (same as the pipeline's file limit). |
| `SCALETRACE_UPLOAD_MAX_MEMBERS` | API | `5000` | Entries in the archive (files and folders). |
| `SCALETRACE_UPLOAD_MAX_DEPTH` | API | `20` | Folder nesting depth of any entry. |
| `SCALETRACE_UPLOAD_MAX_RATIO` | API | `100` | Compression ratio above which a member larger than 1 MB is treated as a ZIP bomb. |
| `SCALETRACE_UPLOAD_TMP` | API | system temp folder | Where the private per-upload temporary folders are created. |
| `SCALETRACE_UPLOAD_TTL_SECONDS` | API | `1800` | How long an upload's results stay in memory. |
| `SCALETRACE_UPLOAD_MAX_STORED` | API | `8` | Uploads kept in memory at once (oldest dropped first). |
| `SCALETRACE_UPLOAD_CONCURRENCY` | API | `2` | Uploads analysed at the same time; more get HTTP 429. |

`python -m scaletrace.api` binds to 127.0.0.1 by default. `--host 0.0.0.0` would expose the API to the network;
do not do that without authentication and the sandboxing work listed below.

## API

| Method and path | Returns |
|---|---|
| `GET /api/health` | status and version |
| `GET /api/projects` | analysable folders, demo flag, whether a recorded runtime report exists |
| `GET /api/models` | v5 learned-ranker checkpoints (`loko_*` diagnostics excluded) |
| `POST /api/analyze` | summary, findings, risk paths, diagnoses, scoring weights, limitations |
| `POST /api/graph` | knowledge-graph nodes and edges, plus layout `level` and `relevant` flags |
| `POST /api/export/{report-html,report-md,diagnosis-json,graph-json,findings-csv,graph-html}` | the same files as the Streamlit Export tab |
| `GET /api/source?project=&file=&line=` | up to 8 lines either side of a finding, from a `.py` file inside the project only |
| `GET /api/uploads/config` | the upload limits in force |
| `POST /api/uploads?filename=&endpoint=&model=` | body: the raw ZIP (`Content-Type: application/zip`); returns the same analysis as `/api/analyze` plus an `upload` block with its id |
| `POST /api/uploads/{id}/analyze?endpoint=&model=` | re-diagnoses the stored parse with another endpoint or ranker (no re-upload) |
| `GET /api/uploads/{id}/graph`, `GET /api/uploads/{id}/export/{format}` | graph and the six exports for an upload |
| `GET /api/uploads/{id}/source?file=&line=` | the excerpt captured around a finding (the files themselves are gone) |
| `DELETE /api/uploads/{id}` | discards an upload's results |

The `POST` bodies are `{"project", "use_runtime", "endpoint", "model"}`. The API has **no endpoint that runs a workload**.
Runtime evidence comes only from an existing `.scaletrace/runtime.json` (recorded with
`scaletrace ml collect-runtime --allow-execute`, which executes the app).

## ZIP upload

**Accepted archives.** A standard, unencrypted `.zip` of a Python/FastAPI project, for example GitHub's
"Download ZIP". The project root is the archive's top level, or the single folder that wraps everything (such
as `repo-main/`); wrapper folders are stripped only while every entry sits inside one folder. At least one
Python file must import FastAPI (`from fastapi import ...` or `import fastapi`).

**What is extracted.** Only `*.py` files and the config files the pipeline reads (`pyproject.toml`,
`setup.cfg`, `setup.py`, `Pipfile`, `alembic.ini`, `requirements*.txt`). Everything else (binaries, data,
nested archives, `.scaletrace/runtime.json`) is counted and skipped, never decompressed. `tests/` folders are
extracted but excluded by the pipeline, as for local projects.

**Rejected, with a plain message and no server paths:** not a ZIP, damaged or truncated archives, encrypted
members, unsupported compression; absolute paths, drive letters, backslashes, `..`, NUL, names ending in a dot
or space, Windows device names (`CON`, `NUL`, `COM1` and so on), duplicate names (case-insensitive); symbolic
links and special files; more entries, deeper nesting, larger files or more extracted bytes than the limits;
suspicious compression ratios; archives without Python files (HTTP 422) or without a FastAPI import (HTTP 422).

**Processing.** Each upload gets its own `tempfile.mkdtemp` folder, which is deleted in a `finally` block on
success and on every failure. The analysis is the same `run_static_analysis` + `diagnose_repository` path as
workspace projects. Uploads are static only: nothing from the archive is imported, run or installed. Analysis
errors return a short message with a reference id; the details go to the server log only. The parse, findings
and excerpts stay in memory under a random id until the TTL, eviction, `DELETE` or a restart.

**Manual check with a real server:**

```bash
.venv\Scripts\python -m scaletrace.api
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/uploads?filename=shop.zip" -H "Content-Type: application/zip" --data-binary @shop.zip
```

Make `shop.zip` from `examples/shop_app` first (for example with Explorer's "Send to > Compressed folder").
The response should list 6 findings and `"upload": {...}`. In the web app, choose **Upload ZIP**, pick the file
and select **Upload and analyse**.

## Screens and their data

All screens use live API data. Nothing is hard-coded or simulated.

- **Overview:** summary counts, risk paths (`risk_paths`), top candidate per diagnosis, the rules applied and the limitations.
- **Findings:** every finding with rule, category, severity, location, function, endpoints, evidence status,
  static evidence snippet, source context (`/api/source`), runtime evidence, false-positive notes and verification steps.
  Search, rule, severity and status filters, and sortable columns.
- **Root causes:** each diagnosis with its symptom, the recorded runtime measurements charted against input size,
  the graph path, the probable cause, ranked candidates (risk, static, runtime, importance and learned score when a model is
  chosen), the scoring formula with the real weights from `scoring`, and the step-by-step reasoning.
- **Knowledge graph:** an SVG layered by the backend's own levels. Nodes can be selected with the mouse or keyboard
  (Tab, Enter, Escape). The panel shows the node's location, rules, metrics and connections.
- **Export:** six downloads generated by the backend. Errors are shown inline.
- **Privacy** (`/privacy`) and **Terms** (`/terms`): drafts that describe how the code behaves, with open items marked.

## Tests and checks

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build
.venv\Scripts\python -m pytest tests/integration/test_api.py -q
.venv\Scripts\python -m pytest -q
```

`tests/integration/test_api.py` covers the API (agreement with the pipeline, exports, input and path-traversal
rejection, and SPA fallback when `dist` is built). `tests/integration/test_uploads.py` covers uploads: end-to-end
results identical to local analysis, follow-up requests, malformed and unsupported archives, traversal, symlinks,
special files, every limit, ZIP-bomb ratio, cleanup after success and failure, no path leaks, and that uploaded
code never runs. `tests/integration/test_ui_smoke.py` still covers the Streamlit fallback.
If pytest reports `PermissionError` on its temp folder, add `--basetemp <a writable folder>`.

## Streamlit fallback

```bash
.venv\Scripts\python -m streamlit run scaletrace/app/streamlit_app.py
```

<http://127.0.0.1:8501>. It is unchanged and still offers the one thing the web app deliberately does not: running a
demo workload after an explicit confirmation.

## Launch checklist (status 29 October 2026)

The app is usable locally. It is **not** ready for public launch.

| Item | Status |
|---|---|
| Favicon (`frontend/public/favicon.svg`, referenced in `index.html`) | Done; served as `image/svg+xml` |
| Platform badge ("Made with ...") | None in the React app (checked in the rendered page). The Streamlit fallback runs with `toolbarMode=minimal`. |
| Privacy policy page | Draft published at `/privacy`; owner details and hosting retention marked "Owner to confirm"; needs legal review |
| Terms page | Draft published at `/terms`; warranty, liability, licence and governing-law sections marked for legal review |
| Direct routes and refresh | Checked for `/`, `/privacy`, `/terms`; unknown paths show a not-found page |
| No fake data or dead controls | Every control calls the API; running workloads from the web app is absent, not shown disabled |
| Responsive and keyboard | Checked at 1366x900 and 375x812 (no horizontal scroll); tabs use arrow keys; graph nodes and findings rows are focusable. No automated accessibility audit has been run. |
| Console errors | None during the checked flows |
| Production build | `npm run build` passes |
| Hosting provider | **Not configured.** No deployment config exists in the repository. |
| Custom domain and HTTPS | **Blocked:** no domain or provider access. Steps: choose a host that can run Python (the API reads local folders, so a static host is not enough); deploy the API with `frontend/dist`; restrict `SCALETRACE_ALLOWED_ROOT` to the demo projects; add the domain at the provider; create the DNS records the provider gives (CNAME or A/AAAA); wait for its TLS certificate; then verify DNS, HTTPS, favicon and routes on the live URL. |
| ZIP upload | Implemented with safe extraction and static analysis only (see "ZIP upload"). Not reviewed by a security specialist; see the remaining risks below. |
| Before any public deployment | Authentication and per-client rate limiting, no `--host 0.0.0.0` without them, a CPU/time limit or separate worker process for upload analysis, a sandbox before any runtime execution, and the legal review above. |

**Remaining upload risks.** No authentication: anyone who can reach the API can upload. The analysis runs in the
API process, with no CPU time limit; the size and file limits bound the work but do not cap parse time. Results sit
unencrypted in process memory, readable by anyone holding the random id until they expire. Only the stdlib ZIP
reader is used, so its own parser bugs are in scope. Python's `ast` parser handles the uploaded source; it
does not execute it. A file too deeply nested to parse is skipped and listed as skipped (checked with 50,000
nested parentheses); parse time itself is not capped.
