# Dev-split log

Policy (agreed 2026-10-25): the benchmark dev split is used for diagnostics and realism checks only --
never for training, early stopping, hyperparameters, variant choice, or choosing between the GNN and the
heuristic. Record here every change to ScaleTrace that was made after looking at dev-split results, so
the final write-up can disclose it.

| Date | What was looked at (dev apps, command) | Observation | Change made (files) | Could it affect test results? |
|---|---|---|---|---|
| — | No benchmark apps existed in Phase 1 (synthetic data only). | — | — | — |
| 2026-10-25 | Static screening of the 34 fetched candidates (before any dev/test split existed) | Routes registered with `router.add_api_route(...)`, cross-module `include_router(..., prefix=...)`, annotated router assignments (`ROUTER: typing.Final = APIRouter()`) and upper-case router names were not detected (bad-cand-012/013, good-c001/c008 showed 0-1 routes) | Generic FastAPI route support in `analyzer/scopes.py` and `analyzer/project.py` (`_resolve_routes`); no repo-specific logic | Yes: endpoint mapping and every graph-based method on these apps. Made before splitting/locking; the change is general FastAPI semantics, not tuned to any label (no labels existed yet). |
| 2026-10-26 | Static analysis of the new controlled copy bad-ctrl-c038-cmp001 (no split existed yet) | Every good-c038 endpoint lacked its `/api` prefix: a parent router's own `APIRouter(prefix="/api")` was ignored when that router was itself included into the app (`app.include_router(api_router)`) | `analyzer/project.py` (`_resolve_routes.effective`) now adds the parent router's constructor prefix; regression test in `tests/unit/test_labelling_workflow.py`; 30 untouched worksheets regenerated | Yes: endpoint names/graph paths on apps with nested prefixed routers. General FastAPI semantics, made before splitting; reviewed apps' endpoints unchanged. The CMP-001 miss on the same copy was deliberately NOT fixed (would be tuning against a benchmark app). |
