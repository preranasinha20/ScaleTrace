# Metric improvement plan (29 October 2026)

Ranked by expected benefit, strength of evidence, cost and regression risk. Based on
docs/evaluation_metrics_audit.md and docs/error_analysis.md. "Done" items are implemented, with tests.

## Evaluation protocol for every change

1. The historical final run (29 October, 11:43:06Z) stays the baseline. Its files are never regenerated.
2. Design and checks use the **dev split** (dry runs), the controlled dev copy, the demo apps and synthetic
   regression tests. Never the locked test split.
3. For each change, record the hit-by-hit dev diff and inspect every added or removed hit in the source.
4. Report dev TP, FP and FN under the frozen labels, the FP breakdown (reviewed or not), and the demo and
   controlled results. State that dev recall cannot be measured (no dev positives).
5. Run the full test suite and `benchmark validate`.
6. Any later test-split run must be **explicitly authorised**, logged as a new run, and described as post-hoc:
   the test split has been inspected since the lock.

## Ranked plan

| Rank | Change | Benefit | Evidence | Cost and risk | Status |
|---|---|---|---|---|---|
| 1 | FP breakdown, reviewed-only sensitivity, app-level bootstrap intervals in evaluation output | separates labelling gaps from errors; shows uncertainty | exact match with worksheets on the historical run | low; additive, official rows unchanged | **Done** (`benchmark/final.py`; `tests/unit/test_evaluation_diagnostics.py`) |
| 2 | DB-001: per-batch work in paged `while` loops is not per-row | removes a clear FP class | dev good-c014, 2 functions, 6 hits; all suggested non-defects | low; strict pattern (non-trivial `.limit(n)` page variable plus a stop on that variable); inner per-row loops still reported | **Done** (`analyzer/loops.py`; `tests/unit/test_paged_loops.py`) |
| 3 | Label work: second-person sign-off; finish good-c014 and good-c030 (including unflagged list queries); settle the 9 unsure test hits | the largest effect on every metric | 42 of 70 test FPs are unreviewed | human time; no code | open (needs reviewers) |
| 4 | Save per-symptom ranking records in final results, and break rules-only ties | enables ranking intervals and tie analysis | Top-1 / MRR gap for rules-only | low | proposed |
| 5 | MEM-001: tag reads filtered by `in_(<list>)` as `bounded_by_input_list` in the evidence (still reported) | lets reviewers and the UI deprioritise them | dev good-c001 (3), good-c014 (2) | low; no change to counts | proposed |
| 6 | DB-001: decide whether a per-iteration `commit()` belongs to DB-001 | clarifies the rule | dev good-c014 (3+) | rule-definition decision first | needs a decision |
| 7 | MEM-001: returned unmaterialised `Query` | possible recall gain | test-split notes only | medium; needs a dev or synthetic case first | proposed, not started |
| 8 | Synthetic memory-growth cases for the ranker, then a new pre-declared evaluation | needed before any learned ranker can be judged on MEM-001 | 39 of 47 real symptoms are memory-growth | high (generator, training, gate) | not recommended now |
| 9 | Cross-module constant tracking for loop caps (`CART_MAX_LINES`) | removes the DB-2 FP class | dev good-c014 (6) | high, risk of hiding real loops | not recommended now |

### Added after the DB-001 second pass (docs/error_analysis.md section 7)

| Rank | Change | Benefit | Evidence | Cost and risk | Status |
|---|---|---|---|---|---|
| 1b | Diagnostic: false positives in the same loop as a matched defect | separates granularity from errors | 4 test-split worksheet notes ("left out to avoid double counting"); dev has no TPs to show it | low; additive, official counts unchanged | **Done** (`benchmark/final.py` `same_loop_companions`; 1 test) |
| 10 | DB-001: do not report an indirect call to an `async def` helper that is **not awaited** (the coroutine never runs) | removes a semantically wrong finding | test-split note only (good-c025 `sales_orders.py:132`); a synthetic probe reproduces it | low; positive test: an awaited call must still be reported | **Done, post-hoc** (owner-approved; `rules/detectors/db_001.py` `discarded_coroutine`; tests in `tests/unit/test_db001_posthoc.py`) |
| 11 | Loops over a **local** name bound once to a small literal list or tuple, never mutated, count as bounded (consistent with the literal and module-constant cases) | consistency | synthetic probe; test-split good-c021 (4); no dev or demo instance | low | **Done, post-hoc** (owner-approved; `analyzer/loops.py` `local_literal_constants`; tests in `tests/unit/test_db001_posthoc.py`) |
| 12 | Decide DB-001 granularity: one finding per call site (now) or one per loop listing all its operations | aligns findings with how reviewers label; fewer duplicate findings in the UI | dev: 13 findings in 5 loops; 4 test worksheet notes | medium: changes finding counts, the graph's per-node rule counts and the features of the trained rankers | **needs a policy decision (Q4)** before any code change |
| 13 | Rule scope for scripts, benchmark harnesses and migrations: entry-point reachability rather than path exclusion | addresses the largest reviewed-FP class (11 test hits) | test worksheets | medium-high: needs entry-point detection | proposed; a policy decision first |

Items 10 and 11 were designed with knowledge of locked test-split notes: any test evaluation after them is post-hoc (docs/error_analysis.md section 8).

## Not to do

- Exclude migrations or scripts by path. That would hide real code in those folders, and it is a denominator
  change rather than a detection improvement.
- Apply AI suggestions to frozen labels to "improve" precision.
- Tune on the test split, or report a re-run there as an independent result.
- Retrain or swap ranking models without new training data that covers memory-growth symptoms.
