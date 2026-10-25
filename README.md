# ScaleTrace

Research prototype that identifies and explains scalability risks in Python/FastAPI backends. It
combines **static source-code evidence**, **runtime execution evidence** and a **knowledge graph**
of the application, and adds a **graph neural network** that learns to rank probable root causes.

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

## Setup

Python 3.10+ (developed on 3.13, Windows).

```bash
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,demo,runtime]"   # core + tests + demo apps + runtime testing
pip install torch --index-url https://download.pytorch.org/whl/cpu   # deep learning (CPU)
pip install -e ".[ml,ui]"              # numpy + Streamlit UI
```

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
streamlit run scaletrace/app/streamlit_app.py
```

Other commands: `scaletrace rules` (rule status), `scaletrace schema` (report JSON Schema).

## Benchmark (team's 25 good + 25 bad apps)

See [docs/benchmark.md](docs/benchmark.md). Static only; benchmark apps are never trained on.

```bash
scaletrace benchmark init benchmark/bad-07 --kind bad --source team
scaletrace benchmark validate benchmark
scaletrace benchmark split benchmark --dev 0.2 --seed 0
scaletrace benchmark lock benchmark
scaletrace evaluate --manifest benchmark/bad-07 ...          # dev apps; test apps need --final (logged)
```

## Deep learning

See [docs/ml.md](docs/ml.md) for the design, the validation gate and the results.
**Current status:** on synthetic held-out data (v5) the pre-declared gate passes, but a simple one-hop MLP ties the GNN, and the real benchmark is not labelled yet. See docs/ml.md.

```bash
scaletrace ml generate data/train --n 100 --style A                     # synthetic labelled apps (v4)
scaletrace ml generate data/test_B --n 30 --style B --seed 9000         # held-out code style
scaletrace ml collect-runtime data/train data/test_B --allow-execute    # runs every generated app
scaletrace ml verify-labels data/train data/test_B                      # keep runtime-verified labels
scaletrace ml audit-data --train data/train --test data/test_B          # shortcut-check suite only
scaletrace ml experiment --train data/train --style-test data/test_B --out results/   # gate + baselines
```

## Tests

```bash
pytest                      # all tests
pytest -m "not runtime"     # skip the test that executes a demo app
```

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
