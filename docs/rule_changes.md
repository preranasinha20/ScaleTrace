# Rule changes after the final run (29 October 2026)

The final test evaluation (docs/benchmark_results.md, logged 2026-10-29T11:43:06Z) is the historical
baseline. It was produced with the rules as they were **before** the changes below, and it has not been
re-run. Any future evaluation of these rules on the same locked test split is post-hoc and is not an
independent evaluation.

## How the changes were chosen

- Only patterns confirmed in source code were changed.
- Rule design and checks used the dev split, the controlled dev copy, the demo apps and synthetic
  regression tests. The locked test apps were not used to design or tune the rules.
- **Disclosure:** the two-step MEM-001 miss was first recorded on good-c003 on 2026-10-26, before the split
  existed (docs/dev_log.md). good-c003 is now in the test split. The same pattern was then confirmed in the
  dev apps good-c001 and good-c032, and the fix was built and checked on those.

## 1. MEM-001: two-step result reads (recall)

**Pattern:** the result of a database call is stored, then materialised on a later line:

```python
result = await session.execute(statement)     # good-c001 BaseRepository.get_where
return result.scalars().all()
```

**Change:** when `.all()` / `.fetchall()` is called on a variable (optionally through `scalars()`,
`unique()`, `mappings()` or `tuples()`), and that variable's latest earlier assignment in the same function
is a database call, the read is reported unless a bound is visible. The evidence records
`form: "two_step"` and `materialized_from`. File: `scaletrace/rules/detectors/mem_001.py` (`_two_step`).

## 2. MEM-001: bounds on statement variables (precision)

**Pattern:** the limit is applied to a statement variable, not inside the call:

```python
select_statement = select(Model).order_by(...).limit(pagination.limit)   # good-c001 count_and_get_many
select_result = await session.execute(statement=select_statement)
objects = select_result.scalars().all()
```

**Change:** for the inline form, the two-step form and `list(...)`, the variables that the database call
receives are resolved through their local assignments, up to 3 levels. A bounding *method call* in any of
these assignments counts as a visible bound. Bounding calls are `.limit()`, `.first()`, `.paginate()` and
the others in `db_bounding_methods`; LIMIT in SQL text also counts. An attribute named `limit`, such as
`params.limit`, does not count.

## 3. MEM-001: INSERT ... RETURNING (precision)

**Pattern:** `select(Model).from_statement(insert(Model).values(rows).returning(Model))` (good-c001
`create_many`). The result holds only the rows just written, so its size is set by the input. This applies
to the inline, two-step and statement-variable forms. `UPDATE ... RETURNING` is still reported.

## 4. DB-001 and all loop-based rules: module-level constant collections (precision)

**Pattern:** a loop over a module constant holding a small literal list, from good-c014's migration
`find_violations`:

```python
CONSTRAINTS = [("products", "ck_product_price_non_negative", "price >= 0"), ...]
for table, name, condition in CONSTRAINTS:
    connection.execute(text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")).scalar()
```

**Change:** a module-level name bound exactly once to a literal list, tuple or set with at most
`bounded_loop_max_iterations` items (default 10) is treated like the literal itself. The name must not be
re-bound, declared `global`, stored to through a subscript or attribute, or mutated (`append`, `extend`,
`add` and similar) anywhere in the module, and it must not be shadowed by a parameter or local variable.
File: `scaletrace/analyzer/loops.py` (`module_literal_constants`, `LoopWalker.register_module`).

## 5. DB-001 and all loop-based rules: paged `while` loops (precision; added during the metrics audit)

**Pattern:** keyset or limit pagination, from good-c014 `_anonymise_pending` and `clear_expired_idempotency_keys`:

```python
while True:
    ids = db.execute(select(Order.id).where(...).order_by(Order.id).limit(batch_size)).scalars().all()
    if not ids:
        break
    db.execute(update(Order).where(Order.id.in_(ids)).values(...))
    db.commit()
```

**Change:** a `while` loop counts as bounded when its direct body assigns a variable from an expression containing
`.limit(n)` (with `n` not the literal 0 or 1), and that variable appears in the loop condition or in the condition
of an `if` whose body is `break`. The loop then runs once per page, not once per row. Loops nested inside it keep
their own context, so per-row queries inside a page are still reported. File: `scaletrace/analyzer/loops.py`
(`paged_loop_variable`). Tests: `tests/unit/test_paged_loops.py` (9).

**Dev effect:** 6 good-c014 DB-001 hits removed (`tasks.py:356, 363, 365, 725, 733, 740`); all six had been suggested
as non-defects. Nothing else changed on the dev apps, the controlled copy or the demo apps. Dev RECORDED went from
0 TP / 35 FP / 0 FN to 0 / 29 / 0.

## 6. DB-001: discarded coroutines of `async` helpers (post-hoc)

An indirect DB-001 finding is skipped when the loop's call to an undecorated project `async def` helper is a bare
expression statement, which never runs. Awaited, scheduled or stored coroutines are still reported. File:
`scaletrace/rules/detectors/db_001.py`. Details in docs/error_analysis.md section 8.

## 7. Loop bounds: local literal constants (post-hoc)

A function-local name bound once to a literal of at most 10 items, and only read in non-mutating, non-escaping
ways, bounds a loop like the literal does. File: `scaletrace/analyzer/loops.py`. Applies to every loop-based rule,
because the walker is shared.

**Post-hoc disclosure.** Both patterns were identified by reading review notes of LOCKED TEST-SPLIT apps (good-c025 `routers/sales_orders.py:132`; good-c021 `app/routers/dashboard.py:55-87`). The changes were made with that knowledge, on the owner's explicit instruction (29 October 2026). The 29 October final run was not re-run, and its results stand as the historical baseline. **Any later evaluation on the test split is post-hoc and not an independent result.** No threshold or other rule was tuned on the test split.

Effect: no finding added or removed on the dev split, the controlled copy, the demo apps or 190 synthetic apps.
Tests: `tests/unit/test_db001_posthoc.py` (25).

## Regression tests

`tests/unit/test_rule_regressions.py` has 24 tests:

- 8 positive MEM-001 cases: two-step `scalars().all()`, `result.all()`, `unique().scalars().all()`, cursor
  `fetchall()`, an unbounded statement variable, `params.limit` used as an attribute, and UPDATE ... RETURNING.
- 9 negative MEM-001 cases: limit inside the select, limit on a statement variable, limit applied later,
  `.first()`, a non-database result, LIMIT in a SQL variable, a reassigned variable, and INSERT ... RETURNING
  in both the two-step and inline forms.
- 4 positive DB-001 cases: a mutated constant, a shadowed name, a re-bound name, and a constant over the size limit.
- 2 negative DB-001 cases: literal list and annotated tuple constants.
- 1 test that the evidence names the result variable.

## What was evaluated

| Check | Before | After |
|---|---|---|
| Rule regression tests (new) | 10 of 21 initial cases failed | 24 of 24 pass |
| Full test suite | 210 pass | 234 pass |
| Demo apps (shop_app, orders_app_defective, orders_app_fixed) | 10 findings | the same 10 findings |
| Controlled dev copy bad-ctrl-c032-db001 | 1 TP, 0 FP, 0 FN | 1 TP, 0 FP, 0 FN |
| Dev split, RECORDED labels (`evaluate-final --dry-run`) | 0 TP, 33 FP, 0 FN | 0 TP, 35 FP, 0 FN |
| `benchmark validate` | VALID | VALID |

**Hit-by-hit dev changes** (each one inspected in the source):

- Removed: good-c014 migration `find_violations` (DB-001). Not a defect: a fixed 10-item constant (exactly the default limit) in a one-off migration.
- Added: good-c001 `get_where`, `list_or_not_found` and `create_superuser_permissions` (MEM-001, two-step).
  All three are bounded in practice: by `id.in_(ids)` from the caller or the request, and by a fixed enum
  of permission actions. Under the frozen labels they count as false positives.
- Not added: good-c001 `count_and_get_many` (limit on a statement variable), `create_many` (INSERT ...
  RETURNING) and good-c032 `get_tasks` (limit inside the select).

**What this does and does not show:**

- On dev, precision did not improve. The new two-step reads found no recorded defect, because the 5 real
  dev apps have no recorded defects. The 33 baseline dev hits are all on good-c014, whose hits were never
  classified.
- Dev recall cannot be measured, because there are no positives.
- The synthetic and regression tests show that the rule now detects the two-step idiom and ignores the
  nearby bounded forms. They do not measure real-world precision or recall.
- No precision, recall or F1 change on real data can honestly be claimed from this work. No accuracy is
  reported, because true negatives are not defined for this benchmark.

## Not changed (and why)

- **Returned unmaterialised `Query` objects:** there is no dev example, so there is nothing to verify a fix against.
- **Excluding migrations and scripts by path:** the constant-loop fix handles the dev case on its merits, and
  a path rule would hide real defects in code that happens to live in those folders.
- **Bounds from `.in_(ids)`, configuration constants (`CART_MAX_LINES`) or slices (`ids[:limit]`):** these
  need value or semantic reasoning that is beyond a small, safe change. They remain documented
  false-positive classes.
- **CMP-001 (passlib hashing):** seen only on a test app, so it was not changed.

Static analysis still only parses code; no target code is imported or executed.
