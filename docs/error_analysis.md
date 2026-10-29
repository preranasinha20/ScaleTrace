# Error analysis (29 October 2026)

Companion to docs/evaluation_metrics_audit.md.

**Sources.**
- **Dev split:** a finding-level comparison of every hit with the source code, using the historical rules and the
  current rules.
- **Test split:** the saved counts plus the review worksheets and notes made before the lock. Test code was
  **not** re-run, and nothing from it was used to design a rule change in this audit.

**Labels.** "Recorded" labels are the frozen AI-assisted draft. Per-item suggestions (revision 4,
docs/label_review_suggestions.md) are AI-assisted and unconfirmed. They are used below only in a clearly named
sensitivity check, never as ground truth.

**Cause types used below:** *detection bug* (the code does not do what the rule says), *rule definition* (the
rule's wording does not settle the case), *missing context* (a bound exists but static analysis cannot see it),
*evaluation* (how it is counted), *labelling* (labels missing or uncertain).

## 1. Where the errors are (test split, historical run)

| App | TP | FP | FN | Notes |
|---|---|---|---|---|
| good-c030 | 0 | 33 | 0 | never classified (partial review); FP is a labelling gap, not a measured error |
| good-c034 | 2 | 12 | 0 | migrations, benchmark scripts, fixed-size loops (from review notes) |
| good-c021 | 7 | 11 | 2 | mixed |
| good-c025 | 6 | 4 | 1 | |
| good-c003 | 0 | 0 | 4 | all misses are list-all endpoints in the two-step async style |
| 10 other apps with hits | 16 | 10 | 3 | bad-cand-013, bad-cand-020, good-c002, c005, c008, c010, c012, c027, c028, c039 |
| 4 apps with misses only | 0 | 0 | 6 | good-c016, c036, c038, c041 (list-all styles MEM-001 did not recognise at the time) |
| 3 apps | 0 | 0 | 0 | good-c007, c011, c035: no hits and no labelled defects |

These three apps hold 80% of the FP, and one app holds 25% of the FN. The pooled numbers therefore say little
about a "typical" app: per-app performance varies widely, and the app-level bootstrap intervals in the audit are wide.

## 2. Rule-by-rule error categories

### DB-001 (database query inside a loop)

| # | Category | Representative evidence | Cause | Status |
|---|---|---|---|---|
| DB-1 | **Paged (batched) `while` loops**: one `.limit(batch_size)` query per page, ending when a page is empty | dev good-c014 `app/tasks.py:352-367` (`_anonymise_pending`), `:724-741` (`clear_expired_idempotency_keys`); 6 hits | rule definition: DB-001 targets per-row queries; a paged loop issues one query per page | **Fixed in this audit** (see docs/rule_changes.md, change 5). Tests: `tests/unit/test_paged_loops.py` (3 negative, 5 positive, 1 evidence) |
| DB-2 | Loops bounded by a configuration cap | dev good-c014 cart loops `app/api/v1/orders.py:289-348`, `app/services/orders.py:118-132`; the cap is `CART_MAX_LINES = 100` in `app/core/config.py:236`, enforced elsewhere; 6 hits | missing context | not fixable safely without cross-module value tracking; kept as a documented FP class |
| DB-3 | Loops over an explicit slice `ids[:limit]` | dev good-c014 `app/services/reconciliation.py:287`; 3 hits | missing context (`limit` is a variable) | not fixed; a slice with a variable bound is not provably small |
| DB-4 | Per-row locked work in self-draining sweeps | dev good-c014 `app/tasks.py:492-517`; 3 hits | rule definition (policy 4: unsure when the code cannot settle it) | no change; labelling decision |
| DB-5 | Loop over a module constant | dev good-c014 migration `find_violations` | missing context | fixed on 29 October (change 4) |
| DB-6 | `db.commit()` counted as a database operation in loops | dev good-c014 `tasks.py:365, 517, 740` | rule definition: a commit is a round trip but not a query | no change; noted in the plan |
| DB-7 | `session.delete(obj)` and `session.add(obj)` not counted | found while writing tests | by design: neither hits the database until a flush | no change; documented |
| DB-8 | Test-split FPs (17 reviewed, 6 unsure) | categories from pre-lock review notes: fixed-size loops, benchmark scripts, migrations (good-c034, c021) | mixed | not used for design |

### MEM-001 (unbounded materialisation)

| # | Category | Representative evidence | Cause | Status |
|---|---|---|---|---|
| MEM-1 | Reads bounded by their input (`where(Model.id.in_(ids))`) or by a fixed enum | dev good-c014 `app/services/orders.py:83-91` (cart ids), good-c001 `repositories.py:95, 205`, `authorization/managers.py:97` | missing context | not fixed: `in_()` on a non-unique foreign key can still be large (good-c014 `orders.py:1685-1694` loads all refunds of the payments). Proposal: report it, but tag the evidence `bounded_by_input_list` |
| MEM-2 | Two-step async reads (`result = await db.execute(...)`, then `result.scalars().all()`) | was the main FN class on the test split (good-c003, c016, c036, c038, c041, from pre-lock notes); confirmed on dev good-c001 and c032 | detection gap | fixed on 29 October, **after** the final run (change 1). Dev effect: +3 hits, all bounded in practice (MEM-1) |
| MEM-3 | `.limit()` applied to a statement variable before execution | dev good-c001 `count_and_get_many`; test good-c027 and c028 (pre-lock notes) | detection gap | fixed on 29 October (change 2) |
| MEM-4 | Returning an unmaterialised `Query` that FastAPI serialises | test notes only | detection gap | not fixed: no dev example to check against |
| MEM-5 | List queries never flagged or reviewed on partially reviewed apps | good-c014 and good-c030 review notes | labelling | recall on those apps is unknown |

### CMP-001, DB-003, MEM-002, EXT-001

- **CMP-001:** 3 test hits are unclassified (good-c030); the dev hit is `json.loads` per row
  (`stripe_events.py:749`), linear work suggested as not a defect. The injected passlib case in the controlled copy
  is missed. Cause: rule definition (the vocabulary includes linear per-item calls). No change.
- **DB-003:** 1 TP, 0 FP on the test split. Too little data to judge.
- **MEM-002 and EXT-001:** no labelled real defects, so they cannot be evaluated at all.

## 3. Dataset-level differences

- **Controlled copies** (team-injected defects, scored separately). Test: 4 TP, 1 FP, 4 FN. Dev: 1 TP. Their other
  labels are inherited from the base apps, so they are not independent of those apps. They are not real-world evidence.
- **Real bad apps:** only two (bad-cand-013, bad-cand-020), both MEM-001. Real DB-001, DB-003 and CMP-001
  positives come from good apps' list-all, N+1 or loop code, or from controlled copies.
- **Dev split:** the 5 real dev apps have **no recorded defects**. Dev recall cannot be measured, and dev
  precision is 0 by construction (every hit counts as an FP). 26 of the current 29 dev hits are on good-c014, never classified.

## 4. Effect of the rule changes on dev

| Rules | Dev hits (RECORDED: TP / FP / FN) | Controlled dev copy | Demo apps |
|---|---|---|---|
| Historical (final run) | 0 / 33 / 0 | 1 TP | 10 findings |
| After 29 October changes 1-4 | 0 / 35 / 0 | 1 TP | same 10 |
| After change 5 (this audit) | 0 / 29 / 0 | 1 TP | same 10 |

**Sensitivity check on good-c014 only (not an official metric).** This uses the AI-assisted revision-4
suggestions, with unsure hits left out:
- Historical rules: 33 hits (9 suggested defects, 19 not, 5 unsure), precision 9/28 = 0.32.
- Current rules: 26 hits (9, 12, 5), precision 9/21 = 0.43.
- All 7 removed hits were suggested non-defects, and no suggested defect was lost.
- good-c001's 3 new hits (MEM-1, bounded) would count as false positives.

This is one app, unconfirmed labels, and no recall. It shows the changes behave as intended; it does not show
a real-world precision gain.

## 5. Label-quality effects

- **Possible labelling gaps:** 42 of the 70 test FPs are not reviewed errors (9 unsure, 33 never classified).
  The reviewed-only sensitivity precision is 0.53, against 0.31 officially. The truth lies between the
  official figure and that bound, as far as these labels can say.
- **Suggestions are not labels:** applying the AI suggestions to good-c030 would turn many of its 33 FPs into TPs.
  That would be a label change made after the lock, need human confirmation, and be post-hoc. It is not done.
- **Shifting denominators:** a change that removes unreviewed hits raises official precision without being
  verified as correct. The new `false_positive_breakdown` makes this visible: it reports reviewed and unreviewed
  FP separately for every future run.

## 6. Root-cause ranking errors (why learned models do not beat rules-only)

| Finding | Evidence | Effect |
|---|---|---|
| **Defect-kind mismatch** | 39 of 47 real symptoms are MEM-001 `memory_growth`. The generator (`ml/generator.py` `DEFECT_KINDS`) has only query-growth and latency-growth kinds | the models never saw a memory symptom in training |
| **No graph-only cases** | in all 47 real symptoms, the root cause is the flagged function itself | the advantage the GNN was designed for (shared-helper cases where only edges reveal the cause) never occurs; a "rank the flagged function first" baseline is close to ideal |
| **Runtime features missing at evaluation** | training loads synthetic apps with `require_runtime=True`, so `runtime:available` was always 1 in training; on the static-only benchmark it is always 0 | out-of-distribution input for every runtime-using model (`gnn_static` and `*_no_code` are the cleanest comparisons) |
| **Ties penalise rules-only Top-1** | expected rank under ties (`higher + (ties + 1) / 2`) | rules-only MEM-001 Top-1 is 0.56 but MRR 0.73. *Hypothesis:* ties with other flagged functions on the same path. It cannot be checked on dev (no dev symptoms), and test records were not saved |
| **Sample size** | 47 symptoms, 3 seeds pooled; no intervals in the saved run | rules_only 0.77 against the best GNN 0.75 is not a meaningful difference |
| **Leakage** | training data is synthetic only; no benchmark app was used for training or early stopping; model choice was fixed before the final run | no benchmark leakage found. Synthetic template families are shared between training and the synthetic gate splits by design (style hold-out covers part of this) |

**Conclusion:** there is no reliable evidence that a model change would help on this benchmark. The real
symptoms do not contain the cases the GNN adds value on, and the main missing ingredient (memory-growth
training cases) would need new synthetic data and a new pre-declared evaluation. No model was retrained.

## 7. DB-001 second pass (29 October 2026, evening)

The test baseline reports DB-001 precision 0.16 and recall 1.00 (7 TP, 37 FP, 0 FN). This section explains what
sits behind it. Treat it as a diagnostic signal: no DB-001 label is independently reviewed.

### 7.1 Label readiness

- **No independent review.** Every worksheet was written by Claude (an AI draft, or the author of the injected
  defects). `second_reviewer` is empty on all 30 kept apps' worksheets, `verification_log.jsonl` has no entries,
  and 0 of 56 recorded defects are source-verified.
- **Undecided hits:** good-c014 (dev, 33 hits) and good-c030 (test, 33 hits) are unclassified. 9 test hits are
  `unsure` (good-c010, c021, c025).
- **What can be measured:** only RECORDED-tier agreement with frozen AI-drafted labels (test split, historical
  run) and finding counts. SOURCE and RUNTIME tiers have no positives. Dev precision and recall cannot be measured
  (no dev positives).

### 7.2 What the 37 test-split DB-001 false positives are

From the pre-lock worksheets. This is a description of existing labels; none of it was used to change a rule.

| Category | Hits | Representative evidence (reviewer note) | Type |
|---|---|---|---|
| Never classified (good-c030) | 14 | partial review | labelling gap |
| One-off scripts, a benchmark harness, an Alembic migration | 11 | good-c034 `benchmarks/db.py:300-496`, `alembic/versions/a71f0c4d9e32_...py:48`; good-c010 `scripts/seed_admin.py:42`; good-c012 `app/scripts/load_data.py:19` | policy: rule scope (code not reachable from an endpoint or job) |
| Fixed-size **local** literal lists | 4 | good-c021 `app/routers/dashboard.py:55-87` (9 categories, 6 months, 8 labels defined in the function) | candidate detection inconsistency (see 7.4) |
| Second query in an already-labelled per-item loop | 4 | good-c010 `tag_service.py:12`; good-c025 `stock_transfers.py:233`, `order_service.py:206, 456` ("part of that defect, left out to avoid double counting") | evaluation granularity: one finding per call site against one defect per loop |
| Async helper called **without `await`** in a loop | 1 | good-c025 `routers/sales_orders.py:132` (creates coroutines, runs no query) | candidate detection bug (see 7.4) |
| Bounded by a configured batch size, or a retry-until-unique loop | 2 | good-c034 `outbox_worker.py:292` (`OUTBOX_BATCH_SIZE`), `oidc.py:168` | missing context |
| Per-user rows without a uniqueness constraint | 2 | good-c021 `dashboard.py:104`, `pages.py:134` (unsure) | label uncertainty |

Only about 7 of the 37 (the async, local-literal and batch-size cases) look like errors that DB-001 itself could
avoid. The rest are a labelling gap (14), a scope policy (11), granularity (4), or uncertainty (2).

### 7.3 Dev evidence (current rules)

- 13 DB-001 hits, all on good-c014, in **5 loops**: `create_order@289` (4), `reconcile_payments@287` (3),
  `expire_unpaid_orders@492` (3), `cancel_order_and_restock@118` (2), `_apply_refund_event` (1).
- Every hit is a real per-iteration database call. The reasons they were suggested as non-defects or unsure are
  context ScaleTrace cannot see statically: `CART_MAX_LINES = 100` set elsewhere, `ids[:limit]` with a variable
  limit, deliberate per-row locking in a self-draining sweep (policy 4), a provider-defined refund count.
- **No dev hit is a detection bug.** The controlled dev copy (1 TP) and the demo apps (1 DB-001 hit each) are correct.

### 7.4 Behaviour probes (synthetic snippets, static parse only)

| Pattern | Current result | Verdict |
|---|---|---|
| two-step `res = await session.execute(...)` in a loop | 1 finding | correct, no duplicate |
| inline `(await db.execute(q)).scalars().first()` in a loop | 1 | correct |
| statement variable `stmt = select(...).where(X.id == i)` in a loop | 1 | correct |
| `self.session.get(...)` in a repository loop | 1 | correct |
| `async for row in await session.stream(...)` with a query inside | 1 | correct |
| `asyncio.gather(*[session.get(X, i) for i in ids])` | 1 | correct |
| `commit()` + `refresh()` per item | 2 | per-call-site by design; a granularity question |
| loop over a local literal list `kinds = ["a", "b", "c"]` | reported | inconsistent with literal and module-constant loops; **not** found in any dev or demo code (only in a `tests/` folder, which is excluded) |
| loop over `in_()` chunks of a list | reported | batching like paging; no dev or demo instance |
| un-awaited `async` helper in a loop | reported (indirect) | semantically wrong (the coroutine never runs); seen only in a test-split note |
| `session.delete(obj)` / `session.add(obj)` in a loop | not reported | by design (no round trip until a flush) |

### 7.5 What was changed

- **No DB-001 detection change.** No dev or demo source shows a confirmed DB-001 bug, and the two candidate
  bugs (local literal lists, un-awaited coroutines) are evidenced only by test-split labels. Fixing them now would
  mean designing from the locked test split. They waited for an explicit decision (metric_improvement_plan.md, items 10-11); both were later approved and implemented post-hoc (section 8).
- **Evaluation diagnostic added:** `false_positive_breakdown.same_loop_as_true_positive` counts false positives that
  share rule, file, function and innermost loop with a matched defect. The official counts are unchanged. Test:
  `tests/unit/test_evaluation_diagnostics.py::test_same_loop_companions_counts_extra_queries_in_a_matched_loop`.
  On dev it is 0 (there are no dev TPs). On the historical test run it would have flagged up to the 4
  granularity cases above. That figure comes from worksheets, not a re-run.

### 7.6 Before and after (dev split, RECORDED tier, frozen labels)

| | Before this pass | After |
|---|---|---|
| DB-001 findings (dev) | 13 (5 loops) | 13 (5 loops) |
| All findings (dev) | 29 | 29 |
| Reviewed / unreviewed FP (dev) | 0 / 29 | 0 / 29 |
| TP / FP / FN (dev) | 0 / 29 / 0 (precision 0 by construction; recall not measurable) | the same |
| Controlled dev copy | 1 TP, 0 FP, 0 FN | the same |
| Demo apps | 10 findings | the same |
| Findings that stopped being detected | none | none |

No metric change is claimed, and none is supported by independently reviewed labels.

## 8. Post-hoc DB-001 fixes (29 October 2026, owner-approved)

**Post-hoc disclosure.** Both patterns were identified by reading review notes of LOCKED TEST-SPLIT apps (good-c025 `routers/sales_orders.py:132`; good-c021 `app/routers/dashboard.py:55-87`). The changes were made with that knowledge, on the owner's explicit instruction (29 October 2026). The 29 October final run was not re-run, and its results stand as the historical baseline. **Any later evaluation on the test split is post-hoc and not an independent result.** No threshold or other rule was tuned on the test split.

### 8.1 Un-awaited `async` helpers in loops

- **Semantics checked:** calling an `async def` function returns a coroutine object and runs none of its body. The
  body runs only when the coroutine is awaited or scheduled. A call that is the whole expression statement
  (`release(db, item.id)`) discards the coroutine, so the helper's queries never execute.
- **What the analyzer knew:** call sites (`CallSite`) record the callee and enclosing loops but not whether the call
  is awaited. The parent node is available through the module context, so DB-001 can tell a discarded call apart.
- **Change** (`scaletrace/rules/detectors/db_001.py`, `discarded_coroutine`): an *indirect* DB-001 finding is skipped
  only when the callee is a project `async def`, has **no decorators**, and the call is a bare expression statement.
- **Still reported:** `await helper()`; `await helper() or ...`; `asyncio.create_task(helper())`;
  `tasks.append(helper())` then `gather`; `gather(*[helper() for ...])`; `pending = helper()`; sync helpers;
  decorated async helpers; and a direct un-awaited query on the handle (handle sync or async-ness is not known).
- **Limitations:**
  - Only the loop's own call site is checked. A discarded call deeper in the call chain still lets the chain reach
    the database.
  - EXT-001's indirect form has the same blind spot and was not changed (it is a different rule).
  - A discarded coroutine is usually a bug in the target app (a forgotten `await`). ScaleTrace does not report it.

### 8.2 Loops over local literal collections

- **How boundedness is decided:** `LoopWalker` marks a loop bounded when its iterable is a literal, a constant
  `range()`, a module constant (29 October) or a paged `while` loop, with at most `bounded_loop_max_iterations` = 10
  iterations. DB-001 skips accesses whose enclosing loops are all bounded.
- **Change** (`scaletrace/analyzer/loops.py`, `local_literal_constants`): a function-local name also counts, if all of
  these hold:
  - it is bound exactly once (nested scopes included) to a literal without `*`;
  - it is not a parameter, `global` or `nonlocal`;
  - every read of it is a loop iterable, a comparison, a subscript read, an argument of a read-only builtin
    (`len`, `sorted`, ...) or a non-mutating method.
  Passing it to any other function, aliasing it, or mutating it disqualifies it.
- **Policy distinction:** a bounded loop can still repeat a database query inefficiently. For example, good-c021's
  dashboard runs 9 aggregate queries per request. That is a constant cost, not one that grows with data, so it is
  outside DB-001's scalability scope (the rulebook's false-positive class "small strictly bounded loops"). It
  remains an efficiency issue a human may still want to fix. Literals longer than 10, and per-item queries in an
  inner loop over real data, are still reported.
- **Shared component:** the loop walker serves every loop-based rule (DB-001, CMP-001, EXT-001, MEM-002, DB-003),
  so the bound applies to all of them, like the literal and module-constant bounds before it. Keeping DB-001-only
  semantics would make the rules disagree about the same loop. No other rule's findings changed on the data below.

### 8.3 Tests

`tests/unit/test_db001_posthoc.py`, 25 tests:
- 11 async cases: 2 suppressed (bare function and method calls); 9 still reported (awaited, inside an expression,
  `create_task`, collected then gathered, gathered comprehension, assigned, sync helper, decorated helper, direct
  un-awaited query).
- 13 loop cases: 3 suppressed (local list, annotated tuple, read by `len`); 10 still reported (parameter, query
  result, appended, `+=` in a loop, re-bound, passed to a function, aliased then mutated, more than 10 items,
  starred, per-item query in an inner loop).
- 1 test that the bound reason names the local constant.

Before the change, exactly the 5 suppression cases and the reason test failed; all 19 "still reported" cases
already passed.

### 8.4 Effect (before and after, findings for all rules)

| Data | Before | After | Added / removed |
|---|---|---|---|
| Dev split, 5 real apps (RECORDED: TP / FP / FN) | 29 findings (0 / 29 / 0) | 29 (0 / 29 / 0) | none |
| Controlled dev copy bad-ctrl-c032-db001 | 1 TP | 1 TP | none |
| Demo apps (shop_app, orders_app_defective, orders_app_fixed) | 10 | 10 | none |
| Synthetic ML apps (190: train, test_styleA, test_styleB) | 7,076 | 7,076 | none |

No finding was added or removed on any non-test data, because neither pattern occurs there. So **no dev or controlled
metric changed**, and no real-world precision, recall or F1 change can be claimed. Effects would show only on the
test split (good-c025 r01, good-c021 r04-r07: up to 5 fewer FPs), and measuring them there would be post-hoc. That
run was not done.
