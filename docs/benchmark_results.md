# Real-benchmark results (final run, 2026-10-29)

One final evaluation on the locked test split, logged in `benchmark_data/apps/final_runs.log`
(2026-10-29T11:43:06Z). Full output: `scaletrace_output/final_test/final_report.md` and `final_results.json`.
Command:

```bash
scaletrace benchmark evaluate-final benchmark_data/apps --models <all 24 v5 checkpoints except loko_*> --out scaletrace_output/final_test
```

## What was evaluated

- **Test split:** 24 apps, locked 2026-10-26: bad-cand-013, bad-cand-020, two controlled copies, and 20 good apps.
  Grouped split (template family / derived-from / author), seed 0. Dev split (6 apps) was used only for a dry run.
- **Static only.** Public repositories were never executed, so there is no runtime evidence.
- **Labels:** the frozen labels as recorded, which come from an AI-assisted review that **no second person has
  verified yet**. They are reported as the **RECORDED** tier. The SOURCE and RUNTIME tiers count only
  person-confirmed defects. Since no sign-off has been made, those tiers have 0 positives, and every hit on a
  recorded defect is held out as "pending" there.
- **Partial labels:** good-c014 (dev) and good-c030 (test) are kept as good apps with their hits unclassified, so
  their real defects are not labelled.

## Detection (rule engine), RECORDED tier, real apps (22)

| | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| All rules | 31 | 70 | 16 | 0.31 | 0.66 | 0.42 |
| Excluding good-c030 (33 unclassified hits) | 31 | 37 | 16 | 0.46 | 0.66 | — |
| MEM-001 | 23 | 30 | 16 | 0.43 | 0.59 | 0.50 |
| DB-001 | 7 | 37 | 0 | 0.16 | 1.00 | 0.27 |
| DB-003 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 |
| CMP-001 | 0 | 3 | 0 | 0.00 | n/a | n/a |

Where the errors come from (from the review notes):

- **False negatives** are mostly list-all endpoints written in styles MEM-001 does not recognise: async
  `select(...)` + `scalars().all()`, two-step `query = ...; query.all()`, or returning an unmaterialised `Query`
  (good-c003, c016, c036, c038, c041). The detection rules were frozen during labelling, so these were not fixed.
- **False positives:** good-c030's unclassified hits (33), known non-defects such as migrations, benchmark
  scripts and fixed-size loops (c034, c021), and a `.limit()` applied to a statement variable that ScaleTrace does not see (c027, c028).

Controlled copies (2 in test, scored separately, not real-world evidence): 4 TP, 1 FP, 4 FN. The injected
DB-003 was found and the injected CMP-001 was missed, both as predicted before the run. The other recorded
defects in the copies are inherited from their base apps.

## Root-cause ranking, RECORDED labels, real apps (47 symptoms from 22 apps)

Models: 3 seeds each, so n = 141 for models and n = 47 for deterministic baselines. Every model was evaluated
**without runtime features** (the benchmark is static-only), and most of them were trained with runtime features.

| Method | Top-1 | Top-3 | MRR |
|---|---|---|---|
| rules_only | 0.64 | 0.87 | **0.77** |
| gnn_combined_no_code | 0.62 | 0.89 | 0.75 |
| heuristic_static | 0.64 | 0.83 | 0.74 |
| gnn_static (trained without runtime) | 0.57 | 0.89 | 0.73 |
| gnn_combined | 0.55 | 0.88 | 0.71 |
| proximity | 0.60 | 0.85 | 0.70 |
| gnn_runtime_only | 0.58 | 0.82 | 0.69 |
| gnn_combined_no_rules | 0.50 | 0.87 | 0.68 |
| gnn_static_no_code | 0.44 | 0.86 | 0.65 |
| mlp_hop | 0.48 | 0.78 | 0.64 |
| mlp_node_only | 0.31 | 0.85 | 0.59 |
| random | 0.00 | 0.55 | 0.38 |

**Reading:**
- No learned model beats the rules-only baseline on the real benchmark. The GNN's synthetic-data advantage (v5 gate) **did not transfer**.
- All methods clearly beat random.
- GNN variants beat the node-only MLP (0.59), so graph context still helps among the learned models.
- No confidence intervals were computed for this run. With 47 symptoms from about 20 apps, the differences between the top methods (0.73–0.77 MRR) are within noise.

## Limitations

- Labels are not independently verified. The RECORDED tier is the AI-assisted draft, and sign-offs can still
  be added after the lock (logged in `verification_log.jsonl`); none had been made at the time of the run.
- The real bad set is small: 2 real bad apps (both MEM-001), plus controlled copies for DB-001, DB-003 and CMP-001.
- Static only: no runtime evidence on real apps, so the research question about static plus runtime evidence is
  answered only on the synthetic and demo apps.
- Two good apps (c014, c030) are partially labelled.

## Changes after this run

The rules were changed after this run (docs/rule_changes.md). The numbers above are the historical baseline with the rules as they were at 11:43:06Z and have not been recomputed. Label-review suggestions for good-c014, good-c030, bad-cand-013 and bad-cand-020 are in docs/label_review_suggestions.md; none has been applied.
