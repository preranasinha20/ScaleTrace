# Team dataset status (Phase 2, 2026-10-25)

Source: https://github.com/preranasinha20/ScaleTrace-Dataset at commit `a159548` (two CSV screening
registers; `manifests/` was empty). Local working copy: `benchmark_data/` (git-ignored).

## What the dataset contains

| File | Rows | What it is |
|---|---|---|
| `good/good_code_25.csv` | 25 | Public FastAPI repositories shortlisted as *reference* candidates. None accepted yet: 17 "needs full source review", 8 "on hold, licence unresolved". 22 of 25 screened from metadata only. |
| `bad/ScaleTrace_GitHub_Candidate_Screening_Register_v2.csv` | 27 | Existing public repositories (mostly team members' own) screened as *defective* candidates. 17 rejected, 5 "modify for benchmark", 3 retained, 2 needs review. |

Missing from both: **commit hashes** (all "Not captured"), **source code**, and **per-defect labels**
(file, function, line range, rule, endpoint, verification). Licences: 10 of 52 count as verified under
a conservative reading (formal LICENSE file seen).

The 10 location-level claims in the bad register (all MEM-001/MEM-002) are kept as **unverified
claims**, not labels. There are no DB-001/DB-003/CMP candidates, so the real benchmark cannot currently
evaluate the repeated-query and computation categories.

## What ScaleTrace did with it

1. `scaletrace benchmark import-register` -> `benchmark_data/registry/benchmark_registry.json` and
   `readiness_report.md` (52 candidates; nothing labelled).
2. `scaletrace benchmark fetch` pinned and downloaded the 35 non-rejected candidates at their current
   HEAD (2026-10-25), static analysis only: **34 fetched**, 1 unavailable (`preranasinha20/seva-pulse`
   asks for credentials: private or deleted). Hooks disabled, `.git` removed, nothing executed.
   A manifest skeleton (no defects) was drafted for each.
3. `scaletrace benchmark screen` -> `screening_report.md`: routes, ScaleTrace findings (reference only),
   and the location of every claim at the pinned commit. Every claim with a location was found in real
   code; most coincide with a ScaleTrace rule hit. 7 fetched repos have no FastAPI route
   (C008, C001, BAD-CAND-003/004/012/017/024).
4. `scaletrace benchmark validate benchmark_data/apps` -> **invalid, as expected**: 9 bad apps have no
   labelled defects; 125 rule hits on good apps await review (`unreviewed_rule_hit`).

## What the team needs to do next

1. For each candidate you keep: review the source at the pinned commit, then either add defects to its
   manifest (bad) or document reviewed rule hits as `known_non_defects` (good). Use
   `screening_report.md` as a starting point, not as labels.
2. Mark each defect's `verification.static` (and `runtime`, or `not_applicable` with a reason). Runtime
   verification of public repos waits for the sandbox.
3. Exclude unsuitable candidates (`split: excluded` + reason), e.g. repos without FastAPI routes.
4. Cover the DB and computation categories: build controlled defective copies of accepted good repos
   (`origin.derived_from`), each defect documented and verified by a person other than its author.
5. Then: `scaletrace benchmark validate` (no errors) -> `split --group-by template_family derived_from author`
   -> `lock` -> `evaluate-final --dry-run` (dev) -> one `evaluate-final` (test).
