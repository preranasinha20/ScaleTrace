# Evaluation metrics audit (29 October 2026)

This audit covers the code as it is in the working tree today (`scaletrace/evaluation/`, `scaletrace/benchmark/`,
`scaletrace/ml/evaluate.py`, `scaletrace/ml/train.py`) and the saved results of the historical final run
(`scaletrace_output/final_test/final_results.json`, logged 2026-10-29T11:43:06Z). The historical report
(`docs/benchmark_results.md`, `scaletrace_output/final_test/`) and `final_runs.log` were not modified. Their
checksums were compared before and after this work.

**How the test split was handled.** The locked test apps were **not re-run** for this audit. A real
`evaluate-final` would add an entry to `final_runs.log`, and skipping the access guard would defeat its purpose.
Test-split numbers below come only from the saved results and from the review worksheets that existed before
the lock. The dev split was re-run with dry runs, which write nothing to `final_runs.log`.

## 1. What is measured

ScaleTrace reports two separate things, and they are never mixed:

| | Detection | Root-cause ranking |
|---|---|---|
| Question | Does a rule finding point at a labelled defect? | Given a slow endpoint, is the labelled root-cause function ranked near the top? |
| Unit | One finding against one labelled defect | One symptom (a labelled defect with an endpoint, a symptom kind and root-cause functions) |
| Code | `evaluation/groundtruth.py` (`match_findings`), `evaluation/metrics.py` (`evaluate`) | `ml/evaluate.py` (`summarize`), `ml/train.py` (`expected_rank`) |
| Metrics | Precision, recall, F1 per rule, per category and overall | Top-1, Top-3, MRR per method, also per defect kind |

### 1.1 Detection: matching

For each application, `match_findings` takes the findings of the rules that ran (`rules_applied`: DB-001,
DB-003, CMP-001, MEM-001, MEM-002, EXT-001) and the labelled defects at the chosen tier:

- A finding **matches** a defect when the rule, file and function (qualified name) are identical and the
  finding's line lies in the defect's `[line_start, line_end]`.
- Matching is **one-to-one and greedy**. Defects are visited in manifest order; each takes the first unmatched
  finding (findings are sorted by file, line, column and rule).
- **TP** is a matched defect-finding pair. **FN** is a defect with no matching finding. **FP** is every finding
  left unmatched. That includes findings on code no reviewer ever classified.
- **True negatives are not defined:** there is no list of code locations known to be safe for each rule. So
  **accuracy and specificity cannot be computed**, and none is reported.

### 1.2 Detection: formulas and aggregation

- Precision = TP / (TP + FP). Recall = TP / (TP + FN). F1 = 2PR / (P + R).
- Each is reported as n/a when its denominator is 0, and F1 as n/a when P or R is n/a or both are 0.
- Counts are **pooled (micro-averaged)** over every finding of every application. Per-rule and per-category
  rows pool the same way. There is no per-app averaging. Per-app TP, FP and FN are saved, but no per-app
  precision is aggregated.
- A false negative is attributed to the rule of the defect, and a TP or FP to the rule of the finding. The
  matching requires the same rule, so no cross-rule credit is possible.

### 1.3 Label tiers and held-out findings

| Tier | Positives | Findings on a recorded but not-yet-verified defect |
|---|---|---|
| RECORDED | every recorded defect not rejected (the AI-assisted draft labels, frozen) | none (all recorded defects are positives) |
| SOURCE | defects a second person confirmed in the code (`static == verified`) | held out as **pending**: neither TP nor FP |
| RUNTIME | SOURCE and confirmed at runtime (or no runtime metric) | held out as pending |

No defect has a second-person sign-off yet, so SOURCE and RUNTIME have **0 positives**. Their precision is 0.00
by definition and their recall is n/a. That means "nothing is verified yet", not a measured precision.
Controlled copies (team-injected defects) are scored in separate tables and kept out of ranking.

### 1.4 How unclear labels are treated (official metric)

| Situation | Official treatment | Consequence |
|---|---|---|
| Hit reviewed as `not_a_defect` (manifest `known_non_defects`) | FP | a reviewed error |
| Hit reviewed as `unsure` | FP (it is not a recorded defect) | may be a labelling gap |
| Hit on a partially reviewed app (good-c014, good-c030: hits never classified) | FP | may be a labelling gap |
| Defect that ScaleTrace never flagged | FN | only counted if a reviewer found and recorded it |
| Unflagged defects on partially reviewed apps | not counted (not recorded) | recall on those apps is unknown |
| Rejected defect | not a positive | its hits count as FP |

### 1.5 Checked edge cases

- **Several findings inside one labelled defect:** under one-to-one matching, the second one would be an FP.
  Checked against every worksheet of the 37 apps: **no** hit lies inside a recorded defect's span without being
  that defect, so this does not affect the current numbers.
- **Several defects at one location:** one finding can satisfy only one defect; the others become FN. No
  overlapping same-rule defects exist in the current labels.
- **Multi-line defects:** handled through `line_end`.
- **Function names:** must match exactly, including the `name@L<line>` form for duplicate handler names. A
  qualified-name mismatch would show up as one FP plus one FN.
- **Greedy versus optimal matching:** greedy can, in principle, assign a finding to a defect that another
  defect needed. That needs overlapping same-rule spans in one function, which do not occur in the current labels.
- **Rules in scope:** all six implemented rules. All labelled defects (MEM-001, DB-001, DB-003, CMP-001) have a detector.

### 1.6 Root-cause ranking

- One record per symptom and method. The candidates are the project functions reachable from the symptom's
  endpoint in the graph.
- Rank = the rank of the best-ranked true root cause, with ties at the **expected rank under random
  tie-breaking** (`higher + (ties + 1) / 2`). A root cause missing from the candidates counts as a miss.
- Top-1 = share of symptoms with rank ≤ 1. Top-3 = share with rank ≤ 3. MRR = mean of 1 / rank, with 0 for a miss.
- **Ties hurt Top-1 more than MRR.** A root cause tied with one other function gets rank 1.5: a Top-1 miss but
  0.67 towards MRR. Rules-only scores (severity-weighted hit counts) tie often; learned scores rarely do.
- Deterministic baselines contribute one record per symptom (n = 47). Each learned method pools 3 seeds (n = 141).
  These are the same 47 symptoms three times, not 141 independent cases.

## 2. Baseline reproduction

| What | How | Result |
|---|---|---|
| Test-split totals, RECORDED, real apps | recomputed from the saved per-app counts | TP 31, FP 70, FN 16; P 0.307, R 0.660, F1 0.419: **identical** to the report |
| "Excluding good-c030" row | the same | P 31/68 = 0.456: identical |
| Dev split, historical rules | dry run using the rule code as of the final run (a copy with the committed `mem_001.py` and `loops.py`) | 0 TP, 33 FP, 0 FN; controlled copy 1 TP: **identical** to the saved 29 October dev dry run |
| Dev split, rules after the 29 October changes | dry run | 0 TP, 35 FP, 0 FN (as documented in docs/rule_changes.md) |
| Test split re-run | **not done** | it would add a final-run log entry; it needs explicit authorisation and would be post-hoc |

## 3. Baseline results (historical final run, RECORDED tier, real apps)

| | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| All rules | 31 | 70 | 16 | 0.31 | 0.66 | 0.42 |
| MEM-001 | 23 | 30 | 16 | 0.43 | 0.59 | 0.50 |
| DB-001 | 7 | 37 | 0 | 0.16 | 1.00 | 0.27 |
| DB-003 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 |
| CMP-001 | 0 | 3 | 0 | 0.00 | n/a | n/a |

**Uncertainty (new, from the saved per-app counts).** 95% percentile intervals, resampling the 22 real
applications with replacement (10,000 resamples, seed 0):

| | Precision | Recall | F1 |
|---|---|---|---|
| All 22 real apps | 0.13 to 0.61 | 0.43 to 0.81 | 0.21 to 0.65 |
| Without good-c030 | 0.30 to 0.68 | 0.43 to 0.81 | 0.38 to 0.68 |

The intervals are wide: with this sample, no precision difference smaller than about 0.2 can be distinguished.

**Where the errors are concentrated.**
- Of the 22 real test apps, 15 have at least one labelled defect and 14 produce any hit.
- 80% of the false positives come from three apps: good-c030 (33), good-c034 (12) and good-c021 (11).

**What the false positives are.** This breakdown is exact: the pre-lock worksheets match the final run's TP and
FP for every real test app.

| FP kind | Count | Meaning |
|---|---|---|
| Reviewed `not_a_defect` (`known_non_defects`) | 28 | reviewed errors |
| Reviewed `unsure` | 9 | undecided |
| Never classified (good-c030) | 33 | labelling gap |

By rule:

| Rule | Defect | Not a defect | Unsure | Never classified |
|---|---|---|---|---|
| DB-001 | 7 | 17 | 6 | 14 |
| MEM-001 | 23 | 11 | 3 | 16 |
| CMP-001 | 0 | 0 | 0 | 3 |

**Sensitivity analysis: reviewed findings only.** This is not the official metric. Unsure and never-classified
hits are left out of the precision denominator:
- Precision = 31 / (31 + 28) = **0.53** (DB-001 7/24 = 0.29; MEM-001 23/34 = 0.68). Recall is unchanged at 0.66.
- The denominator is 59 findings instead of 101.
- This bounds how much of the official precision gap could be a labelling gap. It is not evidence that the
  unreviewed hits are correct.

**Ranking (MRR, n = 47 symptoms from 15 apps):**

| Method | MRR |
|---|---|
| rules_only | 0.77 |
| gnn_combined_no_code | 0.75 |
| heuristic_static | 0.74 |
| gnn_static | 0.73 |
| proximity | 0.70 |
| mlp_node_only | 0.59 |
| random | 0.38 |

No per-symptom records were saved, so no ranking confidence interval can be computed from the historical run.

## 4. Discrepancies and presentation issues found

1. **"47 symptoms from 22 apps"** (docs/benchmark_results.md): the 47 symptoms come from **15** apps. 22 is the
   number of real apps evaluated. Not corrected in the historical file (it is kept unchanged); recorded here.
2. **`ranking_samples` = 45 but n = 47:** `ranking_samples` counts distinct (app, endpoint) pairs. Two endpoints
   carry two labelled defects each, so 45 endpoints give 47 symptoms. Both numbers are correct; only the label
   "symptoms" for the 45 is misleading.
3. **SOURCE and RUNTIME precision shown as 0.00:** correct by the formula, but those tiers have no positives. New
   runs now print an explanatory line (section 6).
4. **Learned-model n = 141** pools three seeds of the same 47 symptoms. Treat it as 47 cases when judging sample size.
5. No metric bug was found. The arithmetic, the pending hold-out (31 real + 4 controlled hits held out at SOURCE)
   and the per-tier FP counts are consistent.

## 5. Limitations of the evaluation itself

- The labels are an AI-assisted draft with **no second-person sign-off**. Every detection number is a RECORDED-tier number.
- Two apps are only partly labelled, and their unflagged defects are unknown. Recall is measured only against
  what reviewers recorded, so it is an **upper bound** with respect to unlabelled misses.
- The **dev split has no positives** in its 5 real apps. Dev data can show which hits a change adds or removes,
  but it cannot measure dev recall, and dev precision is 0 by construction under the frozen labels.
- **Small sample:** 22 test apps, 47 symptoms, wide intervals, no significance claims.
- **Static only:** no runtime evidence on real apps.
- The test split has been **inspected** (worksheets, this audit), so it is no longer an untouched holdout for
  any design decision made after 29 October. Any future test run of changed rules is **post-hoc**.

## 6. Changes to the evaluation code (this audit)

All three are additive diagnostics in `benchmark/final.py`. The official rows are computed exactly as before.

- `false_positive_breakdown`: FP split into `reviewed_non_defect` (matches a `known_non_defects` entry by rule,
  file and line) and `not_reviewed`.
- `sensitivity_reviewed_only`: the reviewed-only precision above, clearly named as a sensitivity analysis.
- `detection_ci95_app_bootstrap`: app-level bootstrap intervals (2,000 resamples, seed 0) for new runs.
- The report gains a "Diagnostics" table and a note that a tier with no positives has no meaningful precision.
- Added later the same day: `false_positive_breakdown.same_loop_as_true_positive`, the number of false positives that
  share rule, file, function and innermost loop with a matched defect (see docs/error_analysis.md section 7).
- Tests: `tests/unit/test_evaluation_diagnostics.py` (6 tests).

The historical `final_results.json` predates these fields and was not regenerated.
