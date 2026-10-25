# Deep learning in ScaleTrace: learned root-cause ranking

## Task

Given a **symptom** on an endpoint (e.g. "SQL queries per request grow linearly with input size"),
rank the functions reachable from that endpoint in the knowledge graph so that the **root cause** --
the function owning the loop that multiplies the work, i.e. where the fix belongs -- comes first.

This is the backtracking step of the diagnosis (Figure 4.4). ScaleTrace has two rankers for it:

1. the transparent heuristic `Risk(v) = w_s S(v) + w_d D(v) + w_p P(v)` (default), and
2. a **graph neural network** (opt-in: `scaletrace diagnose --model gnn.pt`).

The research question for this component: *does a learned model over the knowledge graph combine
static and runtime evidence better than hand-written scoring, and does runtime evidence help it?*

## Model

`scaletrace/ml/model.py` -- a directed, edge-weighted **GraphSAGE** network in plain PyTorch.

* Input encoder (2-layer MLP) -> 3 message-passing layers -> scoring head (MLP over [h, x]).
* Each layer combines a node with the weighted mean of its **predecessors** (callers, the endpoint)
  and, separately, of its **successors** (callees, the database). Direction matters: "a callee's
  queries grow" and "my caller is the symptom endpoint" are different signals.
* Residual connections + LayerNorm; dropout 0.1; hidden size 64.
* Edge weights: 1 + `in_loop` + `observed` (runtime). They also serve as the explanation mask.

Training (`ml/train.py`): listwise ranking loss `-log sum_{root causes} softmax(candidate logits)`,
AdamW, early stopping on validation MRR, feature standardization, and **runtime dropout** (runtime
features zeroed for 15% of training samples, so the combined model degrades gracefully when runtime
evidence is missing).

Explanation (`ml/explain.py`): a GNNExplainer-style optimization of soft edge and feature masks that
keep the predicted candidate ranked first with as little evidence as possible. Reported in
diagnoses as the most influential edges and features.

## Node features (`ml/features.py`)

| Group | Content |
|---|---|
| type | endpoint / function / database / external service |
| structure | size, loops, max loop depth, calls (in loops), DB and external accesses (in loops), unresolved calls, async, handler |
| rules | finding counts per core rule, max severity (raw rule output; never finding *statuses*) |
| symptom | is the symptom endpoint, reachable from it, proximity, symptom kind |
| runtime | measured at the symptom endpoint: executed, call-count growth, attributed-query growth, query share, log calls, log queries, endpoint metric growth |
| code | code embedding of the function source: hashed token bag (default) or a pretrained code Transformer (`--embedder codebert`, CodeBERT, projected to 32 dims) |

Variants (ablations): `static` (no runtime), `combined` (all), `combined_no_rules` (no rule
features), `runtime_only` (type + symptom + runtime).

## Data (`ml/generator.py`)

There is no public dataset of Python backends with labelled scalability root causes, so training data
is **generated** (generator v4, `ml/generator.py`): small FastAPI + SQLAlchemy apps, each with 4 defect
endpoints and 2 decoy/clean endpoints.

* **One fragment pool for every role.** Root causes, hard decoys, orchestrators and helpers are built
  from the same fragments, and every function gets 0-3 random benign fragments.
* **Root vs hard decoy.** Both are "fetch rows, loop, cost operation inside". A root iterates a table
  that grows with the input size; a hard decoy iterates a fixed 3-row table. Which entity name is the
  fixed table is random per app, so static code cannot distinguish the two.
* **Kinds:** `db_query`, `db_helper`, `db_hidden` (static rules miss it), `db_lazy`, `cmp_sort`,
  `cmp_helper`.
* **Structure:** loop depth 0-3; 0-2 decoys and benign helpers attached anywhere on the call chain;
  15% of roots are methods of service classes the static call graph cannot resolve (runtime-observed
  edges recover them); for/comprehension/while loops; SQLAlchemy 1.x and 2.0 query styles; shared
  repository helpers; neutral random names.
* **Labels** come from the generator (never from ScaleTrace's rules). `scaletrace ml verify-labels`
  keeps a label only if its endpoint's metric grows under the cached runtime report, and checks that
  hard decoys stay constant.
* Two code styles; style B is held out.

## Experiment protocol (pre-declared)

`scaletrace ml experiment` (`ml/experiment.py`):

* **Data access:** training apps are loaded with the guard's `training` level (benchmark apps refused);
  test sets with `diagnostic`. Model selection (early stopping) uses 20% of the *synthetic* training
  apps; nothing else is tuned.
* **Splits:**
  - `in_distribution`: held-out seeds, same style (reference only)
  - `style_holdout`: style B (gate split)
  - `leave_one_kind_out`: retrain without each defect kind and test on that kind (gate split)
* **Methods:**
  - non-learned: random, proximity (shortcut detector), rules_only, heuristic_static, heuristic_combined
  - learned, no graph: `mlp_node_only`
  - GNN variants: static, combined (**primary**), combined_no_rules, runtime_only, static_no_code, combined_no_code
* **Statistics:** MRR and Top-1 per seed (mean ± sd over 3 seeds); 95% app-level bootstrap CI of the
  seed-averaged MRR (2000 resamples).
* **Shortcut suite** (`ml/shortcuts.py`, thresholds fixed in code):
  - fails if any single static feature reaches Top-1 ≥ 0.90
  - fails if the node-only MLP reaches Top-1 ≥ 0.90 on the held-out style
  - fails if ≥ 50% of held-out roots have a structural fingerprint seen only on training roots
  - warns on identifier-rename sensitivity > 0.05 MRR, epoch-1 saturation, and near-duplicates ≥ 0.85
* **Validation gate** (all must pass):
  - **C1** the shortcut suite passes
  - **C2** on both gate splits, the primary GNN's CI lower bound is above the CI upper bound of the best
    non-learned baseline and of the node-only MLP
  - **C3** on both gate splits, every primary-GNN seed beats both

Sanity check of the suite itself: on the v3 data it reports **FAIL** (node-only MLP Top-1 1.000,
fingerprint 0.70, all runs saturated at epoch 1), as it should.

## Results

### Experiment log (kept on purpose: negative results are part of the method)

**v1 (discarded): handler shortcut.** In the first generator the root cause was the endpoint handler in
6 of 7 defect kinds, and candidate sets had 2-5 nodes. The GNN reached Top-1 = 1.00 after one epoch.
Fix: loops placed at depth 0/1/2, distractors on the path, and a `proximity` baseline added.

**v2 (discarded): label leak through function names.** Proximity dropped to Top-1 0.35-0.39 (the
handler shortcut was gone), but *every* GNN variant, including static-only and runtime-only, again
reached Top-1 = 1.00, with the best epoch at 1. Cause: the generator named loop owners `collect_*` /
`score_*` and decoys `status_counts_*` / `export_headers_*`, and the code embedding (identifier
sub-words) turned these names into the label. Baselines on v2 for reference (Top-1, held-out style B,
n=100): random 0.00, proximity 0.39, rules_only 0.58, heuristic_static 0.58, heuristic_combined 0.53.

Fix (v3): every generated function gets a neutral random name from one shared pool regardless of its
role; decoy constants get random names per app. Two ablations without the code embedding
(`static_no_code`, `combined_no_code`) were added, so any remaining leak through code text shows up
as a gap between the variants with and without code.

**v3 (discarded): template fingerprints.** Run on 2026-10-25. Every GNN variant, including
`static_no_code` (no code text) and `runtime_only` (no static features), reached Top-1 = 1.00, and
15 of 18 runs had their best epoch at 1. Each template role has a unique per-function signature
(counts of queries, loops, calls), so the model reads the label off single nodes. Baselines on held-out
style B (n=100), Top-1 / MRR: random 0.00 / 0.39, proximity 0.34 / 0.51, rules_only 0.63 / 0.80,
heuristic_static 0.63 / 0.79, heuristic_combined 0.61 / 0.73. There is still **no valid evidence** that
the GNN outperforms the heuristic. Planned fixes (generator v4, shortcut-check suite) are in
`docs/AUDIT_2026-10-25.md`, sections D and G.

**v4 (Phase 1, 2026-10-25): GNN NOT VALIDATED.**

- *Data:* 100 training apps (style A), 25 in-distribution test apps, 30 held-out style-B apps. 608 of
  620 labels runtime-verified (12 CPU labels rejected for insufficient latency growth). All 639 hard
  decoys stayed constant at runtime.
- *Commands:* `ml generate`, `ml collect-runtime`, `ml verify-labels`, then
  `ml experiment --train … --style-test … --indist-test … --extra-test examples/orders_app_defective examples/shop_app`.
- *Full output:* `scaletrace_output/ml_v4/experiment/results.md`.

| Gate criterion | Result |
|---|---|
| C1 shortcut suite | **FAIL**: node-only MLP Top-1 0.955 ≥ 0.90. Other checks passed: single feature 0.579, fingerprint 0.034 (v3: 0.70), rename Δ 0.040, near-duplicate 0.554. Saturation warning on 4/21 runs. |
| C2 style hold-out | **FAIL**: GNN CI [0.968, 0.996] overlaps the node-only MLP's [0.957, 0.992]. It is far above the best non-learned baseline (heuristic_combined [0.600, 0.722]). |
| C3 style hold-out | **FAIL**: seed MRRs 0.984 / 0.976 / 0.992 vs MLP mean 0.976 (seed 2 ties). |
| C2 leave-one-kind-out | **FAIL** by 0.001: GNN CI [0.951, 0.983] vs MLP [0.914, 0.952]. |
| C3 leave-one-kind-out | pass: 0.972 / 0.971 / 0.960 vs MLP 0.933 and heuristic_combined 0.696. |

Held-out style B (30 apps, 118 symptoms), MRR mean ± sd over seeds:

| Method | MRR | Top-1 |
|---|---|---|
| random | 0.257 | 0.000 |
| proximity | 0.340 | 0.153 |
| rules_only | 0.627 | 0.432 |
| heuristic_static | 0.619 | 0.432 |
| heuristic_combined | 0.659 | 0.492 |
| mlp_node_only | 0.976 ± 0.003 | 0.955 |
| gnn_static | 0.841 ± 0.009 | 0.814 |
| gnn_static_no_code | 0.840 ± 0.007 | 0.808 |
| gnn_runtime_only | 0.957 ± 0.004 | 0.921 |
| gnn_combined_no_rules | 0.983 ± 0.004 | 0.969 |
| gnn_combined_no_code | 0.988 ± 0.012 | 0.977 |
| **gnn_combined (primary)** | 0.984 ± 0.008 | 0.972 |

**What this does and does not show**

* *Not shown:* that the graph neural network is needed. A node-only MLP with the same features is
  within noise of the GNN. A follow-up diagnostic (1 seed, style B) gave node-only MLP Top-1 of 0.81
  with static features only (equal to `gnn_static`, so the graph adds nothing in the static setting),
  0.64 with runtime features only, and 0.955 with both.
* *Supported on this synthetic data:* adding runtime evidence helps. gnn_static [0.791, 0.889] vs
  gnn_combined [0.968, 0.996]; heuristic_static 0.619 vs heuristic_combined 0.659. This is
  **partly by construction**: v4's hard decoys are statically identical to roots, so only runtime can
  separate them. It must not be presented as evidence about real applications.
* *Supported on this synthetic data:* the learned models (MLP or GNN) beat the hand-weighted Risk(v)
  heuristic by a wide margin. The heuristic's weakness is concentrated in helper and hidden-handle
  kinds, where query attribution credits the helper rather than the loop owner.
* *Not needed here:* the code embedding (`combined_no_code` ≈ `combined`). Renaming identifiers
  barely moves scores (Δ 0.040), so names no longer leak.
* *Hand-written examples (3 symptoms):* every method is at 1.00. That's too few to discriminate anything.

**Why the graph is not needed yet, and what would test it (proposal, not implemented).** In v4 most
non-root candidates are excluded by their own static shape, and hard decoys are separated by their own
runtime counters. To test the graph, the generator would need cases where only the neighbourhood
reveals the cause:
(a) roots and decoys that call the *same* per-element helper, so the helper's growth is shared and only
the observed call count on each incoming *edge* (N vs 3 per request) tells them apart -- which also
requires passing observed per-edge call counts to the model as edge features;
(b) cheap and bounded loop-with-cost functions among the benign candidates, so static shape alone
cannot rule candidates out.
These are Phase 2 design questions to agree before implementing.

**Lesson recorded for the evaluation chapter:** perfect scores after one epoch are a red flag. Each
leak was found with a trivial baseline or an ablation, which is why both are part of the protocol.

## Threats to validity

* **Synthetic training data.** The generator's patterns are simpler than real code; held-out style B
  and the hand-written examples only partly address this. Results must be re-run on the team's
  benchmark codebases (test only, never train) before any claim.
* **Label convention.** "Root cause = loop owner" favours methods that use call-in-loop structure; a
  different convention (e.g. "function issuing the queries") would change the numbers.
* **Small data, seed variance.** Pooled over 3 seeds; differences of a few points are not meaningful.
* **No causal claim.** A high rank is a probable cause; explanations describe the model, not the code.

**v5 (Phase 2, 2026-10-25): pre-declared gate PASSED on synthetic held-out data -- with important
qualifications.**

*What changed (generator v5 + model):* "shared-helper" cases, where only the graph's connections reveal
the cause. A root and a statically identical *twin* decoy are siblings, both loop over the same growing
table filtered by a module constant, and both call the **same** per-row helper; the root's filter
matches common values (rows grow with size), the twin's matches rare values (at most 3 rows). Their own
static features and runtime counters are the same; only the observed call count on each *edge*
(root->helper grows, twin->helper stays constant) separates them. Also new:

- the kind `db_lazy_helper` (a lazy load hidden in a shared helper)
- benign bounded loops-with-calls
- edge features in the model (edge kind, in-loop, observed, growth of observed calls per request,
  log calls at the largest size), carried by edge-conditioned messages
- two extra baselines *alongside* the unchanged gate: `mlp_hop` (an MLP over the node plus
  hand-engineered one-hop summaries) and `heuristic_combined_edges` (Risk(v) also crediting growing
  outgoing calls)

*Data:* 120 training apps (style A), 30 in-distribution and 40 held-out style-B test apps. 627 of 760
labels runtime-verified, 225 of them verified graph-required. 133 were rejected by the unchanged
verification rule: in 128 shared-helper cases the endpoint's total query/latency growth was diluted by
constant background work (exponent < 0.5). CPU-helper cases were hit hardest, so the graph-required
subset is mostly DB kinds.

| Gate criterion | Result |
|---|---|
| C1 shortcut suite | PASS: single feature 0.492, node-only MLP Top-1 0.837 (< 0.90), fingerprint 0.039, rename Δ 0.031, near-duplicate 0.562; saturation warning 2/24 |
| C2 style hold-out | PASS: GNN CI [0.977, 0.999] vs node-only MLP [0.879, 0.939], best non-learned (rules_only) [0.570, 0.665] |
| C3 style hold-out | PASS: seeds 0.992 / 0.986 / 0.988 |
| C2 leave-one-kind-out | PASS: GNN [0.946, 0.977] vs MLP [0.857, 0.905], rules_only [0.584, 0.660] |
| C3 leave-one-kind-out | PASS: seeds 0.964 / 0.957 / 0.965 |

MRR on the held-out style B (40 apps, 127 symptoms), overall and on the graph-required subset (52):

| Method | MRR overall | MRR graph-required |
|---|---|---|
| rules_only | 0.619 | 0.563 |
| heuristic_combined | 0.605 | 0.258 |
| heuristic_combined_edges (extra) | 0.890 | 0.872 |
| mlp_node_only | 0.910 | 0.819 |
| mlp_hop (extra) | 0.986 | 0.991 |
| gnn_static | 0.815 | 0.780 |
| **gnn_combined (primary)** | **0.989** | **1.000** |

**How to read this honestly**

1. *Supported (synthetic):* neighbourhood information is needed for the graph-required cases. Node-only
   models drop to about 0.82 there while graph-aware models reach about 1.00; edge evidence also lifts the
   non-learned heuristic from 0.26 to 0.87 on that subset.
2. *Not supported:* that the GNN's *learned multi-layer message passing* is what matters. The
   hand-engineered one-hop MLP is statistically tied with the GNN on every split (style hold-out
   [0.970, 0.999] vs [0.977, 0.999]; leave-one-kind-out [0.953, 0.987] vs [0.946, 0.977]). The defensible
   claim is "a learned model over the knowledge graph's one-hop edge evidence", not "a deep GNN is necessary".
3. *By construction:* v5 was designed so that edges are needed, after v1-v4 failed. The gate thresholds
   never changed, but the data was redesigned four times after seeing results (a garden-of-forking-paths
   risk). The result shows the pipeline *can* learn and use edge evidence; it is not evidence about real
   applications.
4. *Runtime still essential:* the static-only GNN reaches 0.815 (0.780 on graph-required).
5. *The real test* is the locked team benchmark, run once with frozen checkpoints. It is static-only
   until the sandbox exists, so the runtime-dependent models cannot yet be evaluated on real code.
