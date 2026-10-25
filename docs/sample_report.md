# ScaleTrace report: orders_app_defective

Generated 2026-10-25T06:10:25+00:00 by ScaleTrace 0.1.0 (rulebook 0.1.0, preliminary).
Runtime evidence: yes.

## Summary

- Python files analyzed: 4 (skipped: 0)
- Findings: 3 - correlated_finding: 1, not_confirmed: 2
- Runtime symptoms: 2; diagnoses: 1; unexplained observations: 0

## Diagnoses

### GET /orders/summary

**Status:** Correlated: static and runtime evidence agree
  
**Ranking:** heuristic

- **Observed symptom:** GET /orders/summary: SQL queries per request grow linearly with input size (11 q @ 10, 101 q @ 100, 1001 q @ 1000; growth exponent 0.96).
- **Also observed:** GET /orders/summary: P95 latency grows linearly with input size (9.0 ms @ 10, 56.8 ms @ 100, 611.2 ms @ 1000; growth exponent 0.92).
- **Affected endpoint/resource:** GET /orders/summary
- **Risk category / rule:** repeated_database_queries / DB-001 Database Query Inside Iterative Loop
- **Source location:** `app/main.py:56` in `list_order_summaries`
- **Severity:** high

**Diagnostic path:** `GET /orders/summary -> list_order_summaries -> Database`

**Step by step:**

1. *(runtime)* Symptom: GET /orders/summary: SQL queries per request grow linearly with input size (11 q @ 10, 101 q @ 100, 1001 q @ 1000; growth exponent 0.96).
2. *(runtime)* Also observed: GET /orders/summary: P95 latency grows linearly with input size (9.0 ms @ 10, 56.8 ms @ 100, 611.2 ms @ 1000; growth exponent 0.92).
3. *(graph)* GET /orders/summary is handled by list_order_summaries() (observed at runtime, 1x per request at the largest size).
4. *(graph)* list_order_summaries() queries Database inside a loop (observed at runtime, 1001x per request at the largest size).
5. *(static)* DB-001 (Database Query Inside Iterative Loop) at app/main.py:56 in list_order_summaries(): `db.query(Order).filter(Order.user_id == user.id).all()` - `db.query(...)` runs on every iteration of `for user in users` (line 55); loop variable(s) `user` feed the query. `db` is treated as a database handle (annotation: Session).
6. *(runtime)* Runtime evidence for DB-001: GET /orders/summary: queries/request attributed to the finding's code 11.0 @ 10, 101.0 @ 100, 1001.0 @ 1000; growth linear (exponent 0.96). Supports the finding.
7. *(runtime)* Runtime evidence for DB-001: Most repeated SQL at the largest size (1000x per request): SELECT orders.id AS orders_id, orders.user_id AS orders_user_id, orders.total AS orders_total FROM orders WHERE orders.user_id = ?
8. *(runtime)* Also present in this function but not confirmed by the workload: MEM-001 at app/main.py:53 (GET /orders/summary: MB (process RSS peak) 72.8 @ 10, 73.7 @ 100, 81.5 @ 1000; growth constant (exponent 0.02). Does not grow with input size under this workload.); MEM-001 at app/main.py:56 (GET /orders/summary: MB (process RSS peak) 72.8 @ 10, 73.7 @ 100, 81.5 @ 1000; growth constant (exponent 0.02). Does not grow with input size under this workload.)
9. *(inference)* Probable cause: DB-001 (Database Query Inside Iterative Loop) in list_order_summaries() at app/main.py:56. Static and runtime evidence agree under the tested workload; this is a probable cause, not a proof of causality.

**Probable cause: DB-001 (Database Query Inside Iterative Loop) in list_order_summaries() at app/main.py:56. Static and runtime evidence agree under the tested workload; this is a probable cause, not a proof of causality.**

**Ranked candidates:**

| Function | S | D | P | Risk | Learned | Rules |
|---|---|---|---|---|---|---|
| `list_order_summaries` | 1.00 | 0.96 | 0.50 | 0.88 | n/a | MEM-001, DB-001 |
| `get_db` | 0.00 | 0.00 | 0.50 | 0.10 | n/a | - |

**Suggested verification:**

- locate database operation
- identify enclosing loop
- determine possible loop cardinality
- measure query count under increasing input size
- compare endpoint latency and database load
- apply the suggested correction and re-run the same workload to check that the growth disappears

## Where scaling problems can occur (knowledge graph risk paths)

- `GET /orders/summary -> list_order_summaries [MEM-001, DB-001] -> Database`

## All findings

| Rule | Severity | Status | Location | Function | Endpoint(s) |
|---|---|---|---|---|---|
| MEM-001 | high | not_confirmed | `app/main.py:53` | `list_order_summaries` | GET /orders/summary |
| DB-001 | high | correlated_finding | `app/main.py:56` | `list_order_summaries` | GET /orders/summary |
| MEM-001 | high | not_confirmed | `app/main.py:56` | `list_order_summaries` | GET /orders/summary |

## Limitations

- Static analysis only: findings are potential scalability risks, not observed failures.
- Database handles are inferred from annotations, FastAPI dependencies, factory calls and, as a fallback, variable names; the evidence records which basis was used.
- The call graph is approximate: calls through dynamic dispatch, callbacks or objects of unknown type are not resolved, so indirect findings and endpoint mappings can be incomplete.
- Loop cardinality is not inferred, except that loops over small literal collections or constant range() calls are treated as bounded and not reported.
- Diagnoses rank candidate code with a proposed heuristic (Risk(v)); the top candidate is a probable cause, not proven causality.
- Without request-level traces, paths come from the approximate call graph plus observed call edges; diagnoses lacking runtime attribution are marked approximate.
- Measurements come from one local process under a synthetic workload; they show behaviour under this workload only, not production behaviour.
- CPU and memory are process-level (psutil): they cannot be attributed to individual endpoints when requests overlap, and memory includes interpreter and library overhead.
- With call tracing enabled, sys.setprofile adds overhead to every Python call, so absolute latencies are inflated; growth with input size remains comparable.
- Query counts and attribution cover SQLAlchemy engines only; other drivers are not instrumented.
- Unavailable runtime metric: external service call counts (HTTP clients are not instrumented).
