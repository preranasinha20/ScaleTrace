# Labelling plan and running status (target: ready Tuesday 2026-10-27)

All repos are in `benchmark_data/apps/<app_id>/`, pinned at the commit ScaleTrace fetched on 2026-10-25
(the commit the screening team looked at was not recorded and stays **unknown**). Nothing in them is
ever executed.

## Prioritised review queue

Effort: S ≈ 10 min, M ≈ 30 min, L ≈ 1–2 h. "Hits" are ScaleTrace rule findings that a reviewer must
classify; they are not labels.

### P1 — Bad candidates with located claims (the real positives; do first)

| App | Repo | What to check | Effort |
|---|---|---|---|
| bad-cand-013 | VinayakFTW/Hail_Mary_Directive | 3 MEM-001 claims (`get_all_users`, `get_all_games`, `get_session_chats`): is each `.all()` really unbounded, with no pagination elsewhere? ScaleTrace flags all 3. Routes use `add_api_route` (needs the route fix first). | M |
| bad-cand-020 | ATS-27/razorpay-VerifAI | 2 MEM-001 claims (`analyze_dispute_endpoint`, `razorpay_webhook`: whole upload or body read, no size cap?); claim at line 700 is not inside a function, so find the real location or drop it. | M |
| bad-cand-012 | VinayakFTW/pulseai | 2 MEM-002 claims (`process_chat`, `save_history`): does `conversation_history` live across requests (module/global/persisted) or per request? ScaleTrace's narrowed MEM-002 does *not* flag it, so decide by reading the code. Routes use `add_api_route`. | M |
| bad-cand-019 | ATS-27/uaide | MEM-001 claim has no location; ScaleTrace has 1 MEM-001 hit to inspect. Label only if you can point to the line. | S |

### P2 — Bad "modify for benchmark" candidates (decide: keep, exclude, or use as a base)

| App | Repo | What to check | Effort |
|---|---|---|---|
| bad-cand-002 | preranasinha20/anomx-fraud-detection-system | 3 routes, no hits; the screening note says the CSV is bounded at 1,000 rows. Most likely **exclude** ("no target-rule defect at the pinned commit"). | S |
| bad-cand-003, -004, -017, -024 | civicpay, equinox, From-Posts-to-Patterns, GSTRecon-AI | 0 FastAPI routes (Flask/Streamlit per the register). After the route fix confirms this: **exclude** ("not a FastAPI application"). | S each |

### P3 — Good, licence verified, quick (accept or reject as reference apps)

| App | Repo | Hits | What to check | Effort |
|---|---|---|---|---|
| good-c038 | udamuri/fastapi-backend-starter-kit | 0 | Confirm FastAPI app; skim the data-access code for target patterns ScaleTrace may miss | S |
| good-c011 | KenMwaura1/Fast-Api-example | 0 | same | S |
| good-c032 | martcpp/task-management-api | 0 | same | S |
| good-c007 | timhughes/example-fastapi-sqlachemy-pytest | 0 | same | S |
| good-c003 | dmitryrep/fastapi-boilerplate | 0 | same | S |
| good-c002 | nikaera/fastapi-sqlalchemy-alembic-pytest-sample | 1 | classify the MEM-001 hit | S |
| good-c039 | preneond/python-fastapi-example | 2 | classify 2 MEM-001 hits | S |
| good-c034 | HoungDev/fastapi-production-api | 14 | classify 10 DB-001 + 4 MEM-001 hits | L |

### P4 — Good, licence NOT verified (needs the licence decision below)

0 hits (quick once the licence is settled): c009, c023, c016, c041, c035, c036.
With hits: c012 (1), c027 (1), c028 (3), c005 (3, incl. one DB-003), c010 (5), c025 (10), c021 (18),
c030 (33), c014 (33).

### P5 — Re-check after the route fix (do not exclude yet)

good-c001 (Kostiantyn-Salnykov/fastapi_quickstart) and good-c008 (modern-python/fastapi-sqlalchemy-template)
show 0 routes, possibly because of the `add_api_route` gap.

## Per-app checklist (what "reviewed" means)

1. **Is it a FastAPI app?** Routes found; entry point identified. If not: `split: excluded`, reason.
2. **Licence:** verified (LICENSE file seen at the pinned commit; name it) or **unknown**. Never guess.
3. **Each claim / ScaleTrace hit:**
   - bad app: real defect? -> a defect entry with rule, file, function, line range, endpoint, and
     `verification.static: verified` only if you confirmed it in the code. `runtime: unverified` (we do
     not run public code; MEM-002/EXT-001 may use `not_applicable` with a reason).
   - good app: harmless? -> `known_non_defects` with a reason. A real target defect -> a defect with a
     description (it becomes a positive).
   - Unsure -> leave it out and note it; never guess.
4. **Two-person rule:** the person who marks `static: verified` is not the one who wrote the claim.

## Workflow (built and tested)

The worksheets for all 34 apps are in `benchmark_data/review/`:
`review_<app>.md` to read, `review_<app>.json` to edit.

1. Open `review_<app>.md` and read the code it shows (and more of the repo if needed). Never run it.
2. Edit only `review_<app>.json`:
   - `reviewer`, `review_date`
   - `app.decision` keep / exclude (+ `exclusion_reason`), `app.fastapi_confirmed`
   - `app.licence`: leave `unknown` unless you saw the LICENSE file; then add `licence_evidence`
   - per item: `decision` = `defect` / `not_a_defect` / `unsure`, plus a `reason`
   - for a defect: `static_verified_by` (who confirmed it in the code; leave empty if nobody did)
     and, if known, `endpoint`, `symptom_kind`, `root_cause_function` (pick from the candidates shown)
   - `runtime` stays `unverified`. `not_applicable` is allowed only for MEM-002/EXT-001, with a note.
3. Apply it:
   ```bash
   scaletrace benchmark apply-review benchmark_data/review/review_bad-cand-013.json --apps-dir benchmark_data/apps
   ```
   It writes only your explicit decisions, refuses unjustified ones (missing reason, licence without
   evidence, exclusion without reason, runtime claims), keeps `unsure` items out, re-validates the app,
   and stores a copy of the review next to the manifest. Re-running it is safe.
4. Freeze (Monday evening):
   ```bash
   scaletrace benchmark validate benchmark_data/apps
   scaletrace benchmark split benchmark_data/apps --dev 0.2 --seed 0 --group-by template_family derived_from author
   scaletrace benchmark lock benchmark_data/apps
   scaletrace benchmark evaluate-final benchmark_data/apps --dry-run --out scaletrace_output/final_dry_run
   ```

Apps not reviewed by the freeze: set `app.decision: exclude`, `exclusion_reason: "not reviewed before freeze"`.

## Running list

**Done**
- Dataset imported (52 candidates); 34 repos pinned and fetched (static only); manifests drafted; screening report.
- `evaluate-final` built and tested on a mock locked benchmark.
- Generator v5 experiment: gate passed on synthetic data; `mlp_hop` ties the GNN (docs/ml.md).

- Route fix: `add_api_route`, cross-module `include_router(prefix=)`, annotated/upper-case routers
  (Hail_Mary 1 -> 11 routes, C001 0 -> 4, C008 0 -> 8); logged in `docs/dev_log.md`.
- Two verification tiers reported separately everywhere (source-confirmed / runtime-verified).
- Drafts record licence and screening commit as UNKNOWN unless verified (34 re-drafted; none had been edited).
- `review-sheet` + `apply-review` built and tested; 34 worksheets generated.
- Decisions (2026-10-25): two tiers with source as primary for the static benchmark; unverified-licence
  repos included for analysis only (licence recorded unknown); apps not reviewed by the freeze are excluded.

- P1 first-pass reviews applied (2026-10-25/26, reviewer "Claude (AI-assisted draft; not a verifier)";
  `static_verified_by` left empty everywhere, so **nothing is source-confirmed yet**):
  - bad-cand-013 keep: 3 MEM-001 defects (`get_all_users`, `get_all_games`, `get_session_chats`).
  - bad-cand-020 keep: 3 MEM-001 defects (whole-upload `await file.read()` in `analyze_dispute_endpoint`,
    `analyze_media_compatibility`, `analyze_media`; the 200 MB limit is client-side only); r03
    (`razorpay_webhook` body read before HMAC) left **unsure**. Two FastAPI apps share `POST /api/analyze`.
  - bad-cand-012 **excluded**: LICENSE.md is proprietary (all rights reserved, no distribution). Decisions kept
    on record: 1 MEM-002 defect (shared, never-truncated `conversation_history.json`), 5 non-defects.
  - bad-cand-019 **excluded**: its `model_service.py` is identical (apart from line endings) to bad-cand-020's; the
    defect is already bad-cand-020-r04.
- ScaleTrace false positive found: CMP-001 treats names reassigned inside a `while` body as loop-invariant
  (pulseai `json.loads(response_str)`). Not fixed yet.
- Validation: 9 -> 5 errors (the 5 left are the P2 apps).
- Controlled copies built (2026-10-26; see `benchmark_data/controlled/README.md`): bad-ctrl-c032-db001,
  bad-ctrl-c002-db003, bad-ctrl-c038-cmp001. One documented patch each, `origin.controlled = true`,
  grouped with their MIT base, `static: unverified`. Expectations were written before running ScaleTrace:
  DB-001 and DB-003 detected; CMP-001 missed (passlib hash not in the vocabulary; left unfixed on purpose).
  `evaluate-final` now reports controlled copies in their own section and leaves them out of ranking.
- Route fix 2: a parent router's own `APIRouter(prefix=)` now applies to routers included into it
  (good-c038 endpoints now carry `/api`). 30 untouched worksheets regenerated; reviewed ones untouched.

**Blockers**
- A second person must confirm each recorded defect in the code (fill `static_verified_by`, re-run
  `apply-review`). Until then the real benchmark has **0 positives** at both tiers.
- No DB-001/DB-003/CMP-001 claims exist in the real bad set. Possible real ones in "good" apps, still to
  review: good-c005 `get_gantt_report` (lazy `t.project` per task, DB-003), good-c025 `receive_goods` and
  others (a query per item inside a loop, DB-001). Licences for both are unverified.

**Remaining (in order)**
1. Second-person check of the P1 defects and the 3 controlled copies (read
   `benchmark_data/controlled/<app>.patch`, fill `static_verified_by`, re-run `apply-review`).
2. Review the copies' bases good-c032, good-c002, good-c038 (P3) first: a rejected base means excluding its copy.
   Then P3 -> P2 -> P4 -> P5 (C001/C008 now have routes).
4. Validate, split, lock, dry run, final evaluation.
5. GNN follow-up: does any case need more than one hop (GNN vs `mlp_hop`)? Exploratory and labelled as such.
6. Explainer fix.
7. Tuesday: UI and end-to-end demo and report check; presentation material.
