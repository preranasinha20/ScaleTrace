# Recording the ScaleTrace benchmark (25 good + 25 bad applications)

This guide is for the team building the benchmark. ScaleTrace only *reads* these files; it never
trains on, tunes against, or (in Phase 1) executes benchmark applications.

## 1. Layout

```
benchmark/                      <- benchmark root (keep it outside scaletrace_output/)
  benchmark_index.json          <- written by `scaletrace benchmark split`, extended by `lock`
  final_runs.log                <- appended by every final evaluation
  good-01/                      <- one directory per application
    scaletrace_benchmark.json   <- the manifest (this guide)
    app/...                     <- the application source
  bad-01/
    scaletrace_benchmark.json
    ...
```

## 2. Workflow

### 2a. From the team's screening spreadsheets (public candidates)

```bash
scaletrace benchmark import-register --good good_code_25.csv --bad bad_register.csv \
    --registry benchmark_data/registry/benchmark_registry.json
scaletrace benchmark fetch  --registry benchmark_data/registry/benchmark_registry.json --apps-dir benchmark_data/apps
scaletrace benchmark screen --registry benchmark_data/registry/benchmark_registry.json --apps-dir benchmark_data/apps
```

* `import-register` keeps the spreadsheet's suspected findings as **unverified claims** in the registry;
  it creates no labels.
* `fetch` pins each non-rejected candidate's commit and downloads it for static analysis only (git hooks
  off, `.git` removed, never executed), then drafts a manifest with no defects. It never overwrites a
  manifest someone has edited.
* `screen` writes a reviewer aid: routes, ScaleTrace findings, and where each claim sits at the pinned
  commit.

Current status of the team's dataset: [dataset_status.md](dataset_status.md).

### 2b. For each application

```bash
scaletrace benchmark init benchmark/bad-07 --kind bad --source team --author alice --template-family orders-v2
```
`init` writes a blank manifest and prints the app's routes and ScaleTrace findings **for reference only**.
Do not copy findings into the manifest: ground truth must come from the authors and reviewers, never from
the tool being evaluated.

Fill in each defect by hand (template: `docs/templates/scaletrace_benchmark.template.json`). Then:

```bash
scaletrace benchmark validate benchmark          # static checks only; exit code 1 on errors
scaletrace benchmark summary benchmark           # counts by kind, split, rule, verification status
scaletrace benchmark split benchmark --dev 0.2 --seed 0   # grouped, stratified dev/test split
scaletrace benchmark validate benchmark          # again, now including the split checks
scaletrace benchmark lock benchmark              # freeze the test split (hashes every test app)
```

After `lock`, any change to a test application, its manifest, or the set of test apps fails validation.
The split cannot be recomputed.

```bash
scaletrace benchmark evaluate-final benchmark --dry-run --out results/dev_rehearsal   # dev split; not logged
scaletrace benchmark evaluate-final benchmark --models frozen.pt --out results/final  # test split; logged
```

`evaluate-final` refuses to run unless the benchmark validates without errors and is locked. It is
static-only (public code is never executed): rule-engine precision/recall/F1 against fully verified
defects, and root-cause ranking (random, proximity, rules, heuristic, and any given frozen models)
for verified defects that name an endpoint, a symptom kind and a root-cause function.

## 3. Manifest fields

| Field | Meaning |
|---|---|
| `app_id` | Unique id, e.g. `bad-07` |
| `kind` | `bad` (contains intentionally injected defects) or `good` (reference, suitable for the selected rules -- not "free of all problems") |
| `origin.source` | `team` (written for ScaleTrace) or `public` (GitHub). Both are protected by the guard |
| `origin.author`, `origin.template_family` | Used to keep related apps in the same split |
| `origin.derived_from` | The `app_id` this app was derived from (e.g. a bad copy of a good app); kept in the same split |
| `origin.repo_url`, `commit`, `license` | Required for public apps |
| `split` | Leave `unassigned`; the index written by `split` is authoritative. Use `excluded` + `exclusion_reason` to drop an app |
| `run` | Workload file and runtime status (`not_attempted` in Phase 1) |
| `defects[]` | One entry per labelled defect (below) |
| `known_non_defects[]` | Rule hits you reviewed and judged harmless (they still count as rule false positives) |

Each defect:

| Field | Meaning |
|---|---|
| `defect_id` | Unique across the whole benchmark, e.g. `bad-07-d1` |
| `rule_id`, `category` | Must match the rulebook (`scaletrace rules`) |
| `file`, `function`, `line_start`, `line_end` | Where the defect is; the line range must lie inside the function |
| `endpoint` | Affected endpoint, exactly as ScaleTrace prints it, e.g. `GET /orders` |
| `symptom_kind` | `query_growth`, `latency_growth` or `memory_growth` (omit if none is expected) |
| `root_cause_functions` | `module:qualname` ids where the fix belongs (`app.routers.orders:list_orders`) |
| `path` | Optional knowledge-graph node ids from endpoint to resource |
| `verification` | `static`: unverified / verified / rejected; `runtime`: unverified / verified / rejected / not_applicable; plus `verified_by`, `date`, `notes` |

**Only fully verified defects count as positives** (static `verified` and runtime `verified` or
`not_applicable`). Unverified defects are reported separately; rejected ones are ignored.
Use `not_applicable` only when no runtime metric exists for the rule (today: EXT-001, MEM-002), and say
why in `notes`.

## 4. What the validator checks

Errors (block evaluation): invalid schema, duplicate app or defect ids, a bad app without defects, a
good app with an unjustified defect, missing file/function, line range outside the function, unknown
rule, rule/category mismatch, unknown endpoint, unknown root-cause function, unknown `derived_from`,
related apps split across dev/test, app missing from the index (or vice versa), split mismatch, and any
change to a locked test app.

Warnings (review by hand): unverified defects, path nodes/edges not in the static graph, a verified
defect that ScaleTrace's own rule does not report, a rule hit on a good app that is neither a defect
nor a known non-defect, and structurally near-duplicate apps in different splits.

## 5. How the splits are used (agreed policy)

| Split | Used for | Never used for |
|---|---|---|
| **dev** (~20%) | Pipeline debugging, rule false-positive review, realism checks (do synthetic graphs look like real ones?) | Training, early stopping, hyperparameters, choosing a model variant, choosing between GNN and heuristic |
| **test** (~80%) | One final evaluation of a configuration fixed in advance | Anything else |

Every change made after looking at dev results is recorded in `docs/dev_log.md`. The test split is read
only by `scaletrace evaluate --manifest --final` or `scaletrace ml evaluate --final`, both of which
require a locked index and append to `final_runs.log`.

Model selection uses held-out *synthetic* data only (`scaletrace ml experiment`).

## 6. Good apps that turn out to contain target defects, or cannot run

* A good app with a confirmed target defect: record it as a defect with a `description` (it becomes a
  positive), or exclude the app (`split: excluded`, `exclusion_reason`). Do not silently drop it.
* An app that cannot be executed stays in the static-only evaluation with `run.runtime_status: failed`
  or `not_attempted`; its defects use runtime `unverified` unless the rule has no runtime metric.
