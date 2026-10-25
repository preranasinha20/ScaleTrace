# Design decisions

| # | Decision | Rationale |
|---|---|---|
| D0 | The central output is a **knowledge graph** of the codebase (endpoints, functions, DB/external resources) with findings attached, showing where scaling problems can occur. | Project owner's goal. |
| D1 | Evaluation keeps the charter's three categories (DB queries, computation, memory). ASY-001 stays an extension. | Charter is the primary scope reference; confirm with the professor. |
| D2 | The rulebook JSON is never modified by ScaleTrace; detector vocabulary lives in `DetectorConfig`. | Keeps the rulebook a stable research artifact. |
| D3 | Code lives in `scaletrace/` at the repo root next to the numbered research folders. | Simple install. |
| D4 | MEM-002 flags only module/global/class-level collections, not local `append` in loops. | Local result-building loops are almost always intended. |
| D5 | ScaleTrace defines the ground-truth (`scaletrace_groundtruth.json`), root-cause (`scaletrace_rootcause.json`) and workload (`scaletrace_workload.json`) formats. | Benchmarks need common, machine-checkable formats. |
| D6 | Dependencies are installed in a repo-local `.venv`. | Isolation. |
| D7 | The assistant does not commit or push; the team does. | Project owner's preference. |
| D8 | EXT-001 is implemented but reported outside the three-category evaluation. | Rulebook core, but not a charter category. |
| D9 | No numeric confidence; severity + status + recorded evidence basis. | Rulebook `finding_policy`. |
| D10 | **Deep learning is a core component**: a graph neural network ranks root-cause candidates on the knowledge graph, using code embeddings, rule outputs, and runtime evidence as node features. | Owner requirement (2026-10-25). Owner did not answer the four DL questions; defaults below were applied. |
| D11 | Default DL setup: GNN (directed GraphSAGE, plain PyTorch) + hashed code embeddings; pretrained Transformer embeddings optional; CPU only. | Small data, no GPU assumed, no heavy graph library. |
| D12 | Training data comes from a **synthetic app generator**; evaluation uses held-out apps (a held-out code style and hand-written examples). The team's benchmark apps should be used **only for testing**, never training. | Avoids train/test leakage; the GNN never trains on the rule engine's labels. |
| D13 | Root-cause label = the function owning the loop that multiplies work (where the fix belongs). | Consistent, actionable label; differs from "the function that issues the queries". |
| D14 | The deterministic `Risk(v)` heuristic remains the default ranker; the GNN is opt-in (`--model`) and always reported side by side with S/D/P. | Interpretability is a research goal; the GNN must earn its place in the evaluation. |
| D15 | Runtime execution requires `--allow-execute` (CLI) or a consent checkbox (UI); the target binds to 127.0.0.1 and workloads have no host setting. | Safe execution requirements from the brief. |
