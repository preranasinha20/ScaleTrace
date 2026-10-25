# Known limitations

## Static analysis

* **Findings are potential risks.** `static_candidate` means the pattern is present, not that it causes
  a measurable problem.
* **Approximate call graph.** Resolved: direct calls, imported functions/modules (incl. relative imports),
  `self.method()`, constructors, `Depends(f)`. Not resolved: dynamic dispatch, callbacks, objects of
  unknown type (e.g. `repo.get()` where `repo` is a parameter), decorators that wrap functions.
  Unresolved calls are counted per function (`unresolved_calls` in the graph).
* **Routes**: decorators on names assigned from `FastAPI()`/`APIRouter()` (or named `app`, `api`,
  `router`, `*router`). `APIRouter(prefix=...)` is applied; `include_router(..., prefix=...)` in another
  module is not.
* **Database handles are inferred**, strongest first: annotation, `Depends(get_db)`, factory call, name.
  Name-only inference can cause false positives/negatives. A handle passed under an unusual,
  un-annotated name (e.g. `store`) is missed -- the synthetic "db_hidden" defect tests exactly this.
* **Loop cardinality is not inferred**, except loops over small literals / constant `range()`. A loop
  over a module-level constant tuple (`for s in STATUSES`) *is* reported; runtime evidence is what
  shows it does not grow (`not_confirmed`).
* **Rule-specific**: MEM-001 reports every unbounded read (including tables small in practice).
  MEM-002 only considers module/class-level collections (decision D4). DB-003 matches relationship
  attributes by name. CMP-001 uses a configurable vocabulary of "expensive" calls. DB-002 and CMP-002
  are not implemented.
* `tests/` and `test_*.py` are excluded by default (`exclude_tests`).

## Runtime testing

* Measures one local process under a synthetic workload; results describe *this workload only*.
* CPU and RSS are process-level; with overlapping requests they cannot be attributed to an endpoint,
  and small datasets rarely move RSS measurably (MEM findings usually stay `not_confirmed`).
* Call tracing (`sys.setprofile`) inflates absolute latencies; growth across sizes stays comparable.
  Generator/coroutine resumptions are de-duplicated per frame (best effort).
* SQL counting and attribution cover SQLAlchemy engines only; external HTTP calls are not counted, so
  EXT-001 cannot be runtime-confirmed. MEM-002 growth across repeated requests is not measured.
* The target runs with the interpreter given in the workload (default: ScaleTrace's). Its dependencies
  must be installed there. Use an isolated environment/container for untrusted code.

## Diagnosis

* `Risk(v)` is a proposed heuristic, not a validated formula. Weights are configurable
  (`--diagnosis-config`). Missing terms are dropped and the diagnosis is marked `approximate`.
* Query attribution credits the innermost project function on the stack when SQL runs. For
  indirect N+1 the helper receives the queries; the *loop owner* (where the fix belongs) is found via
  the call-in-loop edge and static findings, not via attribution alone.
* "Probable cause" is never proof of causality.

## Deep learning (GNN ranker)

* **Training data is synthetic.** The generator covers 6 defect kinds, 3 loop depths, decoys and two
  code styles. Real applications are more varied; generalization beyond the generator is the main
  threat to validity. The hand-written examples are a small sanity check, not an evaluation set.
* The GNN ranks candidates *given a symptom endpoint*; it does not detect symptoms.
* GNNExplainer-style masks explain what the *model* relied on; they do not prove causality.
* The default code embedding is a hashed bag of tokens (not a neural network). The pretrained
  Transformer embedder (`--embedder codebert`) is implemented but was not evaluated in this
  environment (it requires downloading a ~500 MB model).
* Small data: a few hundred samples; results vary by seed (the experiment reports pooled seeds).

## Benchmark tooling (Phase 1)

* Validation is static: it checks that labels point at real code, not that a defect really causes a
  scalability problem. That judgement is the `verification` fields, filled in by people.
* The guard recognises benchmark apps by their manifest (`origin.source` team/public) or by being
  listed in a `benchmark_index.json` up to 4 directory levels above them. An app copied elsewhere
  without its manifest is not recognised -- keep the benchmark in its own folder.
* The lock detects changes to test apps; it cannot stop someone from reading test files directly.
  `final_runs.log` records sanctioned final evaluations only.
* Near-duplicate detection is structural (AST node-type shingles) and approximate.

## Generator v4 and the validation gate

* The fixed-size "decoy" table makes roots and hard decoys statically indistinguishable *by
  construction*. This tests exactly whether runtime evidence helps, but real code has other ways of
  bounding loops (configuration, pagination, business rules) that the generator does not model.
* Label verification uses the same runtime instrumentation that produces features for the model; a
  bug in the instrumentation would affect both. The examples' manual measurements are an independent check.
* The gate's non-overlapping-CI criterion is conservative (stricter than a paired test). With ~25-30
  held-out apps per split it may reject a real but modest improvement; this is the agreed trade-off.

## UI

* Streamlit warns that `components.html` will be replaced by `st.iframe`; the UI uses it to embed the
  graph viewer and needs updating when that API is removed.
* The graph viewer loads vis-network from unpkg (pinned 9.1.9), so it needs internet access.

## Environment notes

* In the sandbox used during development, pytest could not write to the system temp folder, so tests
  ran with `--basetemp` elsewhere. Plain `pytest` works in a normal shell.
