"""Interactive HTML view of the knowledge graph (vis-network, loaded from a pinned CDN URL)."""

from __future__ import annotations

import html
import json
from typing import Any

import networkx as nx

from scaletrace.graph.knowledge_graph import NodeKind, risk_paths
from scaletrace.schemas import Finding

VIS_NETWORK_URL = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"

KIND_LEVEL = {NodeKind.ENDPOINT.value: 0, NodeKind.FUNCTION.value: 1, NodeKind.RESOURCE.value: 9}


def _levels(graph: nx.MultiDiGraph) -> dict[str, int]:
    """Left-to-right layering: endpoints, then functions by call depth, then resources."""
    levels: dict[str, int] = {}
    frontier = [n for n, d in graph.nodes(data=True) if d["kind"] == NodeKind.ENDPOINT.value]
    for node in frontier:
        levels[node] = 0
    depth = 0
    while frontier and depth < 50:
        depth += 1
        next_frontier = []
        for node in frontier:
            for _, target, data in graph.out_edges(node, data=True):
                if graph.nodes[target]["kind"] == NodeKind.FUNCTION.value and target not in levels:
                    levels[target] = depth
                    next_frontier.append(target)
        frontier = next_frontier
    deepest = max(levels.values(), default=0)
    for node, data in graph.nodes(data=True):
        if node not in levels:
            levels[node] = deepest + 2 if data["kind"] == NodeKind.RESOURCE.value else deepest + 1
    return levels


def _vis_data(graph: nx.MultiDiGraph, findings: dict[str, Finding], only_relevant: bool) -> dict[str, Any]:
    relevant = set(graph.nodes)
    if only_relevant:
        relevant = set()
        for node, data in graph.nodes(data=True):
            if data["kind"] == NodeKind.ENDPOINT.value:
                relevant.add(node)
                relevant.update(nx.descendants(graph, node))
            if data.get("findings"):
                relevant.add(node)
    levels = _levels(graph)
    nodes = []
    for node, data in graph.nodes(data=True):
        if node not in relevant:
            continue
        rules = data.get("rules") or []
        label = data["label"] + (f"\n[{', '.join(rules)}]" if rules else "")
        details = {k: v for k, v in data.items() if k not in ("findings",)}
        nodes.append({
            "id": node,
            "label": label,
            "group": data["kind"] + ("-risk" if rules else ""),
            "level": levels[node],
            "details": details,
            "findings": [findings[f].model_dump(mode="json") for f in data.get("findings", []) if f in findings],
        })
    edges = []
    for source, target, data in graph.edges(data=True):
        if source in relevant and target in relevant:
            edges.append({
                "from": source,
                "to": target,
                "label": data["kind"] + (" (loop)" if data.get("in_loop") else ""),
                "kind": data["kind"],
                "dashes": not data.get("observed", False) and not data.get("static", True),
                "observed": data.get("observed", False),
                "in_loop": data.get("in_loop", False),
            })
    return {"nodes": nodes, "edges": edges}


def render_graph_html(graph: nx.MultiDiGraph, findings: list[Finding], title: str, only_relevant: bool = True) -> str:
    finding_map = {f.finding_id: f for f in findings}
    data = _vis_data(graph, finding_map, only_relevant)
    paths = [p.describe(graph) for p in risk_paths(graph)]
    payload = json.dumps(data).replace("</", "<\\/")
    path_items = "\n".join(f"<li><code>{html.escape(p)}</code></li>" for p in paths) or "<li>No endpoint reaches a function with findings.</li>"
    observed = any(d.get("observed") for _, d in graph.nodes(data=True))
    return _TEMPLATE.format(
        title=html.escape(title),
        vis_url=VIS_NETWORK_URL,
        payload=payload,
        path_items=path_items,
        n_nodes=len(data["nodes"]),
        n_edges=len(data["edges"]),
        n_findings=len(findings),
        runtime_note="Runtime evidence merged: thick edges were observed during the workload." if observed
        else "Static graph only: edges are inferred from source code (approximate).",
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScaleTrace graph - {title}</title>
<script src="{vis_url}"></script>
<style>
:root {{ --bg:#f7f7f5; --panel:#ffffff; --text:#1d1d1f; --muted:#6b6b70; --border:#dcdcd8; --accent:#c2410c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17171a; --panel:#222226; --text:#ececef; --muted:#a0a0a8; --border:#3a3a40; --accent:#fb923c; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:16px 20px; border-bottom:1px solid var(--border); }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.meta {{ color:var(--muted); font-size:13px; }}
main {{ display:grid; grid-template-columns: minmax(0,1fr) 360px; gap:0; height: calc(100vh - 70px); }}
#graph {{ border-right:1px solid var(--border); min-height:420px; }}
aside {{ overflow:auto; padding:16px; background:var(--panel); font-size:13px; }}
aside h2 {{ font-size:14px; margin:16px 0 6px; }}
code, pre {{ font-family: ui-monospace, Consolas, monospace; font-size:12px; }}
pre {{ white-space:pre-wrap; background:var(--bg); padding:8px; border-radius:6px; border:1px solid var(--border); }}
.finding {{ border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:6px; padding:8px; margin:8px 0; }}
.legend span {{ display:inline-block; margin-right:10px; }}
.dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px; vertical-align:middle; }}
ul {{ padding-left:18px; }}
@media (max-width: 800px) {{ main {{ grid-template-columns: 1fr; height:auto; }} #graph {{ height:60vh; border-right:none; border-bottom:1px solid var(--border); }} }}
</style>
</head>
<body>
<header>
  <h1>ScaleTrace knowledge graph: {title}</h1>
  <div class="meta">{n_nodes} nodes, {n_edges} edges, {n_findings} findings. {runtime_note}</div>
</header>
<main>
  <div id="graph"></div>
  <aside>
    <div class="legend">
      <span><i class="dot" style="background:#ef4444"></i>endpoint</span>
      <span><i class="dot" style="background:#3b82f6"></i>function</span>
      <span><i class="dot" style="background:#f59e0b"></i>function with findings</span>
      <span><i class="dot" style="background:#10b981"></i>resource</span>
    </div>
    <div id="details"><p class="meta">Click a node to see its details and findings.</p></div>
    <h2>Risk paths (endpoint to code with findings)</h2>
    <ul>{path_items}</ul>
  </aside>
</main>
<script>
const DATA = {payload};
const COLORS = {{
  "endpoint": {{background:"#fecaca", border:"#ef4444"}},
  "function": {{background:"#dbeafe", border:"#3b82f6"}},
  "function-risk": {{background:"#fde68a", border:"#d97706"}},
  "resource": {{background:"#d1fae5", border:"#10b981"}},
  "endpoint-risk": {{background:"#fecaca", border:"#ef4444"}},
}};
const nodes = new vis.DataSet(DATA.nodes.map(n => ({{
  id:n.id, label:n.label, level:n.level, shape:"box", margin:8,
  color: COLORS[n.group] || COLORS["function"], borderWidth: n.group.endsWith("-risk") ? 3 : 1,
  font: {{ face:"ui-monospace, Consolas, monospace", size:13, multi:false }}
}})));
const EDGE_COLORS = {{ routes_to:"#ef4444", depends_on:"#8b5cf6", calls:"#64748b", queries:"#10b981", calls_external:"#0ea5e9" }};
const edges = new vis.DataSet(DATA.edges.map((e, i) => ({{
  id:i, from:e.from, to:e.to, arrows:"to", label: e.in_loop ? "in loop" : "",
  color: {{ color: EDGE_COLORS[e.kind] || "#64748b" }}, width: e.observed ? 3 : 1.2,
  dashes: e.dashes, font: {{ size:10, align:"middle" }}
}})));
const network = new vis.Network(document.getElementById("graph"), {{nodes, edges}}, {{
  layout: {{ hierarchical: {{ direction:"LR", sortMethod:"directed", levelSeparation:240, nodeSpacing:90 }} }},
  physics: false, interaction: {{ hover:true }}
}});
const byId = Object.fromEntries(DATA.nodes.map(n => [n.id, n]));
function esc(s) {{ return String(s).replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]); }}
network.on("click", params => {{
  const el = document.getElementById("details");
  if (!params.nodes.length) {{ el.innerHTML = '<p class="meta">Click a node to see its details and findings.</p>'; return; }}
  const n = byId[params.nodes[0]];
  let out = `<h2>${{esc(n.details.label)}}</h2><pre>${{esc(JSON.stringify(n.details, null, 2))}}</pre>`;
  for (const f of n.findings) {{
    out += `<div class="finding"><b>${{esc(f.rule_id)}} ${{esc(f.rule_name)}}</b> (${{esc(f.severity)}}, ${{esc(f.status)}})<br>`
        + `<code>${{esc(f.file)}}:${{f.line}}</code><pre>${{esc(f.static_evidence.map(e => e.snippet + "\\n" + e.description).join("\\n\\n"))}}</pre></div>`;
  }}
  el.innerHTML = out;
}});
</script>
</body>
</html>
"""
