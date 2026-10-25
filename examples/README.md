# Examples

Small demonstration applications for ScaleTrace. They are **demos and test fixtures**, not
the evaluation benchmark (the team's benchmark codebases are kept separately).

| App | Purpose |
|---|---|
| `orders_app_defective/` | FastAPI + SQLAlchemy app with one injected DB-001 (query inside loop) in `GET /orders/summary`. |
| `orders_app_fixed/` | Same API, with the per-user query replaced by one aggregate query. |

Each app has a `scaletrace_groundtruth.json` listing the findings ScaleTrace is expected to report.

`scaletrace_config.example.json` shows how to override detector settings (`--config`).

## Analyze (static, nothing is executed)

```bash
scaletrace analyze examples/orders_app_defective --json scaletrace_output/defective.json
```

## Run a demo app (executes its code; needs `pip install -e ".[demo]"`)

```bash
cd examples/orders_app_defective
uvicorn app.main:app --port 8001
```

Then `POST /seed?users=100&orders_per_user=5` and `GET /orders/summary`.
