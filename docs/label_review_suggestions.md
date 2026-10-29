# Label review suggestions (29 October 2026, revision 4)

**Status:** these are AI-assisted suggestions, not decisions or verifications. No worksheet, manifest, label or
lock was changed. Each item needs a human reviewer, and a second person must sign off before a defect counts
as source-confirmed.

The per-item list, with file, line, code behaviour, suggested decision, reason and limitations, is in
`benchmark_data/review/suggestions_2026-10-29.json` (revision 4). Earlier revisions are kept unchanged as
`suggestions_2026-10-29.r1.json`, `.r2.json` and `.r3.json`.
Every suggestion comes from reading the source around the hit, not from the rule ID or file name.

Applying any suggestion to a locked test app would change frozen labels. That needs the owner's explicit
approval, and `apply-review` refuses label changes on locked apps anyway.

## Policy questions

- **Q1, answered by the user on 2026-10-29:** N+1 queries and unbounded reads in **background jobs** count as
  defects when the scalability risk is supported by evidence. Recorded in docs/labelling_plan.md (policy 3).
  How the evidence was read for these suggestions:
  - *Supported:* the set grows with accumulated data, and no limit, batch or cap is visible.
  - *Not supported:* the set is capped, or it is set by something that does not accumulate (for example the
    admins of one realm).
- **Q2, answered by the user on 2026-10-29:** loading rows only to compute totals is not automatically a
  defect. It counts when the code unnecessarily materialises a potentially growing, unbounded result set in
  application memory. Recorded in docs/labelling_plan.md (policy 5).
- **Q3, answered by the user on 2026-10-29:** background sweeps that **drain their own backlog** are not
  automatically defects. They count only when the source code gives evidence of a scalability risk; when the
  code alone cannot settle it, the item is unsure. Recorded in docs/labelling_plan.md (policy 4).

## Summary

| App | Split | Items | Defect | Not a defect | Unsure |
|---|---|---|---|---|---|
| good-c014 | dev | 33 | 9 | 19 | 5 |
| good-c030 | test | 33 | 19 | 10 | 4 |
| bad-cand-013 | test | 3 | 3 (agrees with recorded) | 0 | 0 |
| bad-cand-020 | test | 4 | 3 (agrees with recorded) | 0 | 1 (agrees with recorded) |

Change from revision 1, Q1 items only: c014 now has 3 more defects (r19, r20, r29). c030 now has 10 more
defects (r05, r06, r07, r09, r10, r11, r15, r16, r20, r23) and 4 more not-a-defect items (r08, r17, r22, r24).

## Q3 items (revision 3)

Every one of the eight drains its own backlog. Only one has code evidence of a risk.

| App | Item | Rule | Location | Suggestion | Evidence |
|---|---|---|---|---|---|
| good-c014 | r25 | MEM-001 | app/tasks.py:480 | unsure | Runs every 5 min (`beat_schedule`) and cancels every NEW unpaid order older than 30 min (`UNPAID_ORDER_EXPIRE_MINUTES`, config.py:414), so the set is drained. Only ids are loaded, but nothing caps a run, and the expiries per interval are not visible in code. |
| good-c014 | r26 | DB-001 | app/tasks.py:493 | unsure | One `SELECT ... FOR UPDATE SKIP LOCKED` per order, a deliberate correctness design (docstring). Each order is handled once, with no repeated work. Each iteration also sends an email synchronously (tasks.py:525), so whether a run keeps up cannot be established from code. |
| good-c014 | r27 | DB-001 | app/tasks.py:499 | unsure | Restock per order, with per-order work capped by `CART_MAX_LINES = 100`. The number of orders per run is the open point (r26). |
| good-c014 | r28 | DB-001 | app/tasks.py:517 | unsure | One commit per order, so no transaction stays open through SMTP (comment at tasks.py:500-504). Not repeated work; the run size is open (r26). |
| good-c030 | r18 | MEM-001 | db/job.py:373 | unsure | Hourly (app.py:508-509, via `job_cleanup`, db/job.py:527). It loads deleted users with no jobs or results and hard-deletes them, so the set is drained. Users become purgeable only after the 62-day job purge. No cap, and the per-run count is not visible. |
| good-c030 | r19 | DB-001 | db/job.py:383 | unsure | One link DELETE per purgeable user, over r18's drained set, not a growing set. Whether this is material cannot be established from code. |
| good-c030 | r21 | MEM-001 | db/job.py:414 | **defect** | Repeated unnecessary work. `deletion_date` = creation + 7 days (db/models.py:308-311). Every hourly run reloads every non-deleted job due within 24 h (db/job.py:413-418), so each job is loaded about 24 times. Already-warned jobs are skipped only after a separate per-job `NotificationsSent` query in a new session (utils/notifications.py:290-293). Limitations: the size follows daily job volume, not accumulated data; the repetition is a constant factor of about 24x; the cost is not measured. |
| good-c030 | r25 | MEM-001 | db/job.py:487 | unsure | Loads full rows of jobs older than 62 days and deletes each, so the set is drained hourly. The run size follows job creation 62 days earlier, which the code does not bound. No accumulation or repeated work is visible for this read. It is also the purge that limits the r20 set. |

## Q2 items (revision 4)

All three load rows only for totals, and no bound on the loaded set is established, so all three are suggested
defects. A database-side aggregate is a possible alternative, not a guaranteed improvement.

| App | Item | Location | Suggestion | Evidence |
|---|---|---|---|---|
| good-c014 | r10 | app/api/v1/users.py:657 | **defect** | POST /users/addresses loads every active address of the user as full rows, and uses them only in `not active_addresses` (line 665), an emptiness test. The next query already uses `.limit(1)` (lines 668-674). No cap on addresses per user was found; deletes are soft (`is_active = False`, line 817). Limitations: growth is per user, only through that user's own requests, and the size is not measured (likely small). |
| good-c030 | r02 | db/customer.py:755 | **defect** | Loads every user of the customer's realms as full rows. They are used only for aggregates: `len(users)` (line 835), ids for the jobs query (line 776), and `username.isnumeric()` to split minute totals (line 792). No cap on users per realm is visible. The id list also becomes an IN list of the same size (line 780). Repeated once per customer. |
| good-c030 | r04 | db/customer.py:778 | **defect** | Loads every transcription job of those users since the first day of the previous month as full rows. The loop (lines 789-810) only counts and sums, and also filters status in Python, so jobs that are never counted are loaded too. The date filter limits time (about two months), not the number of rows. Repeated once per customer. |

Correction found during this review: `_customer_get_statistics_sync` (r02 and r04) is called only from the
background jobs `check_quota_alerts` (db/customer.py:873) and `send_weekly_usage_reports` (line 1006).
Revision 3 said r02 is reached from the r01 and r30 endpoints; that was wrong. Those endpoints use a separate
async `customer_get_statistics` (db/customer.py:316), which has the same pattern but is not one of the review
items. It may be worth adding as a reviewer item.

## Suggested defects

| App | Items | Rule | Behaviour |
|---|---|---|---|
| good-c014 | r01 | MEM-001 | all variants of a product, no limit (admin.py:702) |
| good-c014 | r06, r07, r08 | MEM-001 | all payments, refunds and disputes of an order, no limit (orders.py:1681-1694) |
| good-c014 | r09 | MEM-001 | whole Category table, with a Redis cache in front (products.py:189) |
| good-c014 | r19, r20 | MEM-001 | anonymisation job loads all payments and every webhook event of the erased users (stripe_events.py:720, 740) |
| good-c014 | r29 | MEM-001 | low-stock job loads every product at or below the threshold (tasks.py:628) |
| good-c030 | r01, r30 | DB-001 | per-customer statistics queries in CSV export and GET /admin/customers |
| good-c030 | r14, r29 | DB-001 | per-group lookups and statistics in GET /admin/groups |
| good-c030 | r27, r28 | DB-001 | per-job `job_remove` when deleting or updating a user |
| good-c030 | r05, r06, r07 | MEM-001, DB-001 | quota-alert job: all fixed-plan customers, statistics per customer, admin query per realm |
| good-c030 | r09, r10, r11 | MEM-001, DB-001 | weekly report: all customers, all admins, statistics per customer |
| good-c030 | r15, r16 | MEM-001, DB-001 | group quota job: all groups with a quota, admin query per group |
| good-c030 | r20, r23 | MEM-001, DB-001 | cleanup reloads every job past its deletion date, including ones already deleted and kept for audit, then queries JobResult per job (db/job.py:409, 444) |
| good-c030 | r21 | MEM-001 | hourly cleanup reloads every job due within 24 h and re-checks each with a separate query, about 24 times per job (db/job.py:414) |
| good-c014 | r10 | MEM-001 | loads all of a user's active addresses only to test whether any exist (users.py:657) |
| good-c030 | r02, r04 | MEM-001 | statistics helper loads all realm users and two months of their jobs only to count and sum (db/customer.py:755, 778) |

## Unresolved

- Q3 items the code cannot settle (unsure): c014 r25, r26, r27 and r28; c030 r18, r19 and r25. All are drained
  sweeps with no cap and no visible bound on the per-run size.
- c014 r18: the number of refunds per webhook event depends on the payment provider's payload.
- c030 r26: the number of admins per realm is not visible in code.
- c030 r03 (suggested not a defect): assumes `username` is unique; the model was not checked.
- bad-cand-020 r03: whether a webhook body needed for HMAC counts as "required and bounded".

The partial-review notes say good-c014 and good-c030 also contain list queries that ScaleTrace did **not**
flag. Those were not part of this review, so recall on these two apps remains unknown.
