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
* **Loop cardinality is not inferred**, except loops over small literals / constant `range()` and, since
  2026-10-29, over a module-level name bound once to a small literal list/tuple/set that the module never
  changes (`for s in STATUSES`). Mutation from *another* module through an import is not tracked. A function-local name bound once to such a literal, and only read in non-mutating ways, counts too (post-hoc, 29 October); mutation through an alias that escapes the checks is not tracked. Bounds
  from configuration (`CART_MAX_LINES`), slicing (`ids[:limit]`) or business rules are not inferred.
* **MEM-001 bounds** are syntactic: a `.limit()`-style call in the chain, in a statement variable the call
  receives, LIMIT in SQL text, or INSERT ... RETURNING. Reads bounded by their input (`where(id.in_(ids))`)
  or by a fixed enum are still reported.
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

## Real-benchmark evaluation (2026-10-29)

- Labels are an AI-assisted draft frozen as ground truth; no second-person sign-off existed at the final run.
  Results are reported as the RECORDED tier and must be described as such.
- The real bad set is tiny (2 apps, MEM-001 only); DB-001/DB-003/CMP-001 positives come from 3 controlled copies.
- good-c014 and good-c030 are partially labelled (hits unclassified).
- At the final run, MEM-001 missed the two-step style (`result = await db.execute(...)`, then
  `result.scalars().all()`) and returned `Query` objects; CMP-001 missed passlib hashing. Rules were frozen
  during labelling. The two-step miss was fixed AFTER the final run (docs/rule_changes.md); the final-run
  numbers do not include that fix, and any re-run on the same test split would be post-hoc.
- On real code the GNN does not beat the rules-only ranking (static-only, no CIs, n = 47 symptoms).
- The UI's graph view needs internet access (vis-network from unpkg) and uses Streamlit's deprecated
  `components.v1.html` (Streamlit pinned < 1.67).
