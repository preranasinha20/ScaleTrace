# ScaleTrace

**Repository:** <https://github.com/preranasinha20/ScaleTrace>

Research prototype that identifies and explains scalability risks in Python/FastAPI backends. It
combines **static source-code evidence**, **runtime execution evidence** and a **knowledge graph**
of the application, and adds a **graph neural network** that learns to rank probable root causes.

## Quick start (run it locally in about 5 minutes)

You need **Git**, **Python 3.10 or newer** and **Node.js 18 or newer** (which includes npm).

| | macOS | Windows |
|---|---|---|
| Git | `xcode-select --install` (or `brew install git`) | <https://git-scm.com/download/win> |
| Python | <https://www.python.org/downloads/> or `brew install python` | <https://www.python.org/downloads/> (tick **Add python.exe to PATH**) |
| Node.js | <https://nodejs.org> (LTS) or `brew install node` | <https://nodejs.org> (LTS) |

Check them in a terminal: `git --version`, `python3 --version` (Windows: `py --version`), `node --version`.

**1. Get the code**

```bash
git clone https://github.com/preranasinha20/ScaleTrace.git
cd ScaleTrace
```

**2. Start the web app with one command**

macOS or Linux (Terminal):

```bash
sh scripts/run_local.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
```

The script creates a virtual environment in `.venv`, installs ScaleTrace with its web API, builds the
frontend and starts the server. When it prints `ScaleTrace is running`, open <http://127.0.0.1:8000>.
Press `Ctrl+C` to stop it. Running the script again reuses `.venv` and starts faster.

**3. Try it.** The `examples/shop_app` demo opens by default. Choose **Upload ZIP** to analyse a ZIP of your
own FastAPI project; its code is only parsed, never run.

To use another port: `PORT=8010 sh scripts/run_local.sh` (macOS/Linux) or
`powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -Port 8010` (Windows).

### Manual setup (if you prefer not to use the script)

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[api]"          # ScaleTrace + the web API
npm --prefix frontend ci
npm --prefix frontend run build
python -m scaletrace.api                   # http://127.0.0.1:8000
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1                 # if blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install --upgrade pip
python -m pip install -e ".[api]"
npm --prefix frontend ci
npm --prefix frontend run build
python -m scaletrace.api                   # http://127.0.0.1:8000
```

After the first setup you only need to activate the environment and run `python -m scaletrace.api`
(rebuild the frontend only when `frontend/` changes).

### Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found` (macOS) | Install Python from python.org or `brew install python`, then open a new terminal. |
| `python` opens the Microsoft Store (Windows) | Use `py` instead, or install Python from python.org with **Add to PATH** ticked. |
| `npm: command not found` | Install Node.js LTS from <https://nodejs.org>, then open a new terminal. |
| PowerShell says running scripts is disabled | Use the `powershell -ExecutionPolicy Bypass -File ...` command above, or `Set-ExecutionPolicy -Scope Process Bypass`. |
| `Address already in use` / port 8000 busy | Start on another port (see above), or stop the other program using 8000. |
| The page says it cannot reach the API | The server is not running: start it again and keep that terminal open. |
| Blank page or `index.html` missing | Run `npm --prefix frontend run build` again. |

**What is not in the repository.** The research benchmark (`benchmark_data/`, third-party apps) and trained
model files (`scaletrace_output/`) are not published. The web app, the CLI, the demo projects and the test
suite work without them. The learned (GNN) ranker shows up only if you train models yourself
([docs/ml.md](docs/ml.md)); otherwise the heuristic ranking is used.

> Status: research prototype. Six core rules, the knowledge graph, runtime testing, correlation,
> backtracking diagnosis, reports, evaluation, the GNN ranker and a Streamlit UI are implemented.
> See [docs/implemented_vs_planned.md](docs/implemented_vs_planned.md) and
> [docs/limitations.md](docs/limitations.md).

## What it does

```
codebase --> static rules --> knowledge graph --> (opt-in) runtime test --> correlation
                                    |                                           |
                                    +------> backtracking diagnosis <-----------+
                                              (Risk(v) heuristic or GNN ranker)
                                                       |
                         report: symptom -> path -> source line -> probable cause
```

Example (`examples/orders_app_defective`, with runtime evidence):

```
[CORRELATED] GET /orders/summary: SQL queries per request grow linearly with input size
             (11 q @ 10, 101 q @ 100, 1001 q @ 1000; growth exponent 0.96).
  path: GET /orders/summary -> list_order_summaries -> Database
  Probable cause: DB-001 (Database Query Inside Iterative Loop) in list_order_summaries()
  at app/main.py:56. Static and runtime evidence agree under the tested workload; this is a
  probable cause, not a proof of causality.
```

## Full developer setup (CLI, tests, Streamlit, deep learning)

The Quick start installs only what the web app needs. For everything else, activate the environment first
(macOS/Linux: `source .venv/bin/activate`; Windows: `.venv\Scripts\Activate.ps1`), then:

```bash
python -m pip install -e ".[api,dev,demo,runtime,ui]"   # web API, tests, demo apps, runtime testing, Streamlit
```

Deep learning (optional, CPU is enough):

```bash
# macOS (Intel or Apple Silicon)
python -m pip install torch
# Windows and Linux (CPU-only wheels)
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
# then, on every system
python -m pip install -e ".[ml]"
```

Developed with Python 3.13 on Windows. The code has no platform-specific parts, and the same commands
work on macOS and Linux.

## Web app (TypeScript frontend, preferred)

A React + TypeScript frontend (`frontend/`) talks to a thin FastAPI adapter (`scaletrace/api/`) that calls the
same pipeline as the Streamlit UI. It never runs project code. Run it with the Quick start above. Development
mode (hot reload), configuration and the API are described in [docs/frontend.md](docs/frontend.md).

Besides the demo projects, the web app accepts a ZIP of a FastAPI project
(**Upload ZIP**): Python and config files are extracted safely into a temporary folder, analysed statically and
deleted. Nothing from the archive is run. Accepted structure, limits and configuration are in
[docs/frontend.md](docs/frontend.md#zip-upload).

## Demo (Streamlit fallback UI)

The Streamlit UI remains available as a fallback. From the repository root, with the environment active and
the `ui` extra installed (see Full developer setup):

```bash
python -m streamlit run scaletrace/app/streamlit_app.py
```

Then open <http://127.0.0.1:8501>. `.streamlit/config.toml` keeps the server local-only (127.0.0.1) and headless.

1. **Project:** pick a demo project (`examples/shop_app` is the default; `orders_app_defective` and
   `orders_app_fixed` form a before/after pair). Benchmark repositories are never offered in the UI.
2. **Runtime evidence:** the demo apps ship a cached runtime report, which is used by default. "Run workload now"
   executes the app's code and needs the confirmation checkbox.
3. **Ranking:** keep the heuristic ranking, or pick a v5 GNN checkpoint. Optionally enter a slow endpoint.
4. Tabs:
   - **Overview:** risk paths and the top root causes.
   - **Knowledge graph:** interactive; click nodes.
   - **Root causes:** the ranked candidates with evidence steps.
   - **Findings:** filterable, with code snippets.
   - **Export:** HTML/Markdown report, diagnosis/graph JSON, findings CSV, graph HTML.

The graph view loads vis-network 9.1.9 from unpkg, so it needs internet access. The flow is checked
headlessly by `tests/integration/test_ui_smoke.py`.

## Usage

```bash
# 1. Static analysis (never executes the target)
scaletrace analyze examples/shop_app --json scaletrace_output/analysis.json

# 2. Knowledge graph: graph.json + interactive graph.html
scaletrace graph examples/shop_app --out scaletrace_output/shop

# 3. Controlled runtime test (EXECUTES the target; needs scaletrace_workload.json)
scaletrace run examples/orders_app_defective --allow-execute --out scaletrace_output/orders

# 4. Diagnosis: JSON + Markdown + HTML reports
scaletrace diagnose examples/orders_app_defective --runtime scaletrace_output/orders/runtime.json --out scaletrace_output/orders
scaletrace diagnose examples/shop_app --endpoint "GET /customers/report"      # static-only, reported symptom
scaletrace diagnose examples/shop_app --run --allow-execute --model gnn.pt    # with the learned ranker

# 5. Detection metrics against ground truth (precision / recall / F1)
scaletrace evaluate examples/orders_app_defective examples/orders_app_fixed examples/shop_app --csv metrics.csv

# 6. UI
streamlit run scaletrace/app/streamlit_app.py      # local only (see .streamlit/config.toml)
```

Other commands: `scaletrace rules` (rule status), `scaletrace schema` (report JSON Schema).

## Benchmark (team's 25 good + 25 bad apps)

See [docs/benchmark.md](docs/benchmark.md). Static only; benchmark apps are never trained on.
**Results of the final run (2026-10-29):** [docs/benchmark_results.md](docs/benchmark_results.md). Labels are AI-assisted
and not yet independently verified; the test set is locked; sign-offs are logged ([docs/labelling_plan.md](docs/labelling_plan.md)).

```bash
scaletrace benchmark init benchmark/bad-07 --kind bad --source team
scaletrace benchmark validate benchmark
scaletrace benchmark split benchmark --dev 0.2 --seed 0
scaletrace benchmark lock benchmark
scaletrace evaluate --manifest benchmark/bad-07 ...          # dev apps; test apps need --final (logged)
```

## Deep learning

See [docs/ml.md](docs/ml.md) for the design, the validation gate and the results.
**Current status:** on synthetic held-out data (v5) the pre-declared gate passes, but a simple one-hop MLP ties the GNN.
On the real static-only benchmark (recorded labels) **no learned model beats the rules-only baseline** (MRR 0.73 for
`gnn_static` vs 0.77 for rules_only; no confidence intervals were computed, n = 47 symptoms). See docs/ml.md and docs/benchmark_results.md.

```bash
scaletrace ml generate data/train --n 100 --style A                     # synthetic labelled apps (v4)
scaletrace ml generate data/test_B --n 30 --style B --seed 9000         # held-out code style
scaletrace ml collect-runtime data/train data/test_B --allow-execute    # runs every generated app
scaletrace ml verify-labels data/train data/test_B                      # keep runtime-verified labels
scaletrace ml audit-data --train data/train --test data/test_B          # shortcut-check suite only
scaletrace ml experiment --train data/train --style-test data/test_B --out results/   # gate + baselines
```

## Tests

With the environment active and the `dev` extra installed (see Full developer setup):

```bash
python -m pytest                      # all tests
python -m pytest -m "not runtime"     # skip the test that executes a demo app
```

Tests that need an optional extra (Streamlit, torch, the demo apps' dependencies) are skipped when it is missing.

## Repository layout

| Path | Contents |
|---|---|
| `scaletrace/` | application ([docs/architecture.md](docs/architecture.md)) |
| `tests/` | unit tests (rules, graph, diagnosis, ML) and integration tests |
| `examples/` | demo apps with ground truth, root-cause labels and workloads |
| `docs/` | architecture, ML, limitations, decisions, implemented vs planned, schema, sample finding |
| `01_scope/`, `02_initial_rules/` | project charter and preliminary rulebook (research artifacts) |

## Finding statuses

| Status | Meaning |
|---|---|
| `static_candidate` | A rule matched the source. No claim that a failure occurred. |
| `runtime_observed` | A measurable anomaly was observed without a matching static finding. |
| `correlated_finding` | Static and runtime evidence support the same suspected issue. |
| `not_confirmed` | The code ran under the workload, but the rule's metric did not grow. |

Findings never carry confidence percentages. Runtime testing executes target code: only run
repositories you trust, preferably in an isolated environment.
