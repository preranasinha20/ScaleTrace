import { useMemo, useState } from "react";
import type { GraphData, GraphEdge, GraphNode } from "../types";
import { Notice, SeverityBadge } from "./ui";

const NODE_W = 172;
const COL_GAP = 64;
const ROW_H = 54;
const PAD = 20;
const MAX_LABEL = 24;

interface Placed extends GraphNode {
  x: number;
  y: number;
  h: number;
}

interface DrawnEdge {
  source: string;
  target: string;
  kinds: string[];
  observed: boolean;
  inLoop: boolean;
  edges: GraphEdge[];
}

const truncate = (s: string) => (s.length > MAX_LABEL ? `${s.slice(0, MAX_LABEL - 1)}…` : s);

function layout(nodes: GraphNode[], edges: DrawnEdge[]) {
  const levels = [...new Set(nodes.map((n) => n.level))].sort((a, b) => a - b);
  const column = new Map(levels.map((l, i) => [l, i]));
  const preds = new Map<string, string[]>();
  for (const e of edges) preds.set(e.target, [...(preds.get(e.target) ?? []), e.source]);
  const row = new Map<string, number>();
  const placed: Placed[] = [];
  let maxRows = 0;
  for (const level of levels) {
    const col = nodes.filter((n) => n.level === level);
    // Barycentre ordering: place each node near the rows of its predecessors to reduce crossings.
    const bary = (n: GraphNode) => {
      const rows = (preds.get(n.id) ?? []).map((p) => row.get(p)).filter((r): r is number => r !== undefined);
      return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.MAX_SAFE_INTEGER;
    };
    col.sort((a, b) => bary(a) - bary(b) || a.label.localeCompare(b.label));
    col.forEach((n, i) => {
      row.set(n.id, i);
      const h = n.rules && n.rules.length ? 40 : 30;
      placed.push({ ...n, x: PAD + column.get(level)! * (NODE_W + COL_GAP), y: PAD + i * ROW_H, h });
    });
    maxRows = Math.max(maxRows, col.length);
  }
  return {
    placed,
    width: PAD * 2 + levels.length * NODE_W + (levels.length - 1) * COL_GAP,
    height: PAD * 2 + maxRows * ROW_H,
  };
}

export function GraphView({ graph }: { graph: GraphData }) {
  const [onlyRelevant, setOnlyRelevant] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  const { nodes, edges } = useMemo(() => {
    const nodes = graph.nodes.filter((n) => !onlyRelevant || n.relevant);
    const ids = new Set(nodes.map((n) => n.id));
    const merged = new Map<string, DrawnEdge>();
    for (const e of graph.edges) {
      if (!ids.has(e.source) || !ids.has(e.target) || e.source === e.target) continue;
      const key = `${e.source}\u0000${e.target}`;
      const m = merged.get(key) ?? { source: e.source, target: e.target, kinds: [], observed: false, inLoop: false, edges: [] };
      m.kinds.push(e.kind);
      m.observed ||= !!e.observed;
      m.inLoop ||= !!e.in_loop;
      m.edges.push(e);
      merged.set(key, m);
    }
    return { nodes, edges: [...merged.values()] };
  }, [graph, onlyRelevant]);

  const { placed, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const byId = useMemo(() => new Map(placed.map((n) => [n.id, n])), [placed]);
  const sel = selected ? byId.get(selected) ?? null : null;
  const neighbours = useMemo(() => {
    const s = new Set<string>();
    if (sel) for (const e of edges) if (e.source === sel.id || e.target === sel.id) s.add(e.source === sel.id ? e.target : e.source);
    return s;
  }, [sel, edges]);

  if (graph.nodes.length === 0) return <Notice tone="info">The knowledge graph is empty: no endpoints or functions were found.</Notice>;

  const hidden = graph.nodes.length - nodes.length;
  return (
    <div>
      <div className="toolbar">
        <label className="checkbox">
          <input type="checkbox" checked={onlyRelevant} onChange={(e) => setOnlyRelevant(e.target.checked)} />
          <span>Only nodes reachable from an endpoint or with findings</span>
        </label>
        <span className="small muted">
          {nodes.length} nodes, {edges.length} connections{hidden > 0 ? ` (${hidden} hidden)` : ""}
        </span>
      </div>
      <div className="legend" aria-hidden>
        <span><i style={{ background: "#eef4fb", borderColor: "#9fb6d1" }} />Endpoint</span>
        <span><i />Function</span>
        <span><i style={{ background: "#eff8ff", borderColor: "#84caff" }} />Resource</span>
        <span><i style={{ background: "#fef3f2", borderColor: "#b42318" }} />Has findings</span>
        <span><i className="line" style={{ borderColor: "#0f766e" }} />Observed at runtime</span>
        <span><i className="line" style={{ borderColor: "#b6c2cf", borderTopStyle: "dashed" }} />Inside a loop</span>
      </div>
      <div className="graph-layout">
        <div className="graph-canvas">
          <svg width={width} height={height} role="group" aria-label="Knowledge graph. Use Tab to move between nodes and Enter to select.">
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill="#94a3b8" />
              </marker>
            </defs>
            {edges.map((e) => {
              const a = byId.get(e.source)!;
              const b = byId.get(e.target)!;
              const forward = b.x > a.x;
              const x1 = forward ? a.x + NODE_W : a.x + NODE_W / 2;
              const y1 = forward ? a.y + a.h / 2 : a.y + a.h;
              const x2 = forward ? b.x : b.x + NODE_W / 2;
              const y2 = forward ? b.y + b.h / 2 : b.y;
              const dx = Math.max(30, Math.abs(x2 - x1) / 2);
              const d = forward
                ? `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
                : `M${x1},${y1} C${x1 + 40},${y1 + 30} ${x2 + 40},${y2 - 30} ${x2},${y2}`;
              const hl = sel && (e.source === sel.id || e.target === sel.id);
              const cls = ["gedge", e.observed && "observed", e.inLoop && "loop", hl && "hl", sel && !hl && "dim"].filter(Boolean).join(" ");
              return (
                <path key={`${e.source}-${e.target}`} d={d} className={cls} markerEnd="url(#arrow)">
                  <title>{`${a.label} ${e.kinds.join(", ")} ${b.label}${e.observed ? " (observed at runtime)" : ""}${e.inLoop ? " (inside a loop)" : ""}`}</title>
                </path>
              );
            })}
            {placed.map((n) => {
              const isSel = n.id === selected;
              const dim = sel && !isSel && !neighbours.has(n.id);
              const cls = ["gnode", n.kind, n.findings?.length ? "risk" : "", isSel ? "selected" : "", dim ? "dim" : ""].join(" ");
              return (
                <g
                  key={n.id}
                  className={cls}
                  transform={`translate(${n.x},${n.y})`}
                  tabIndex={0}
                  role="button"
                  aria-pressed={isSel}
                  aria-label={`${n.kind} ${n.label}${n.rules?.length ? `, rules ${n.rules.join(", ")}` : ""}`}
                  onClick={() => setSelected(isSel ? null : n.id)}
                  onKeyDown={(ev) => {
                    if (ev.key === "Enter" || ev.key === " ") {
                      ev.preventDefault();
                      setSelected(isSel ? null : n.id);
                    } else if (ev.key === "Escape") setSelected(null);
                  }}
                >
                  <title>{n.label}</title>
                  <rect width={NODE_W} height={n.h} rx={4} />
                  <text x={10} y={19}>{truncate(n.label)}</text>
                  {n.rules && n.rules.length > 0 && <text className="rule" x={10} y={33}>{n.rules.join(", ")}</text>}
                </g>
              );
            })}
          </svg>
        </div>
        <NodeDetails node={sel} graph={graph} edges={edges} onSelect={setSelected} />
      </div>
    </div>
  );
}

function NodeDetails({ node, graph, edges, onSelect }: { node: GraphNode | null; graph: GraphData; edges: DrawnEdge[]; onSelect: (id: string | null) => void }) {
  const label = (id: string) => graph.nodes.find((n) => n.id === id)?.label ?? id;
  if (!node) {
    const risky = graph.nodes.filter((n) => n.findings && n.findings.length);
    return (
      <div className="detail">
        <h3>Node details</h3>
        <p className="small muted">Select a node to see its location, findings and connections.</p>
        {risky.length > 0 && (
          <>
            <div className="section-label">Functions with findings</div>
            <ul className="plain small">
              {risky.map((n) => (
                <li key={n.id}><button type="button" className="btn btn-link mono" onClick={() => onSelect(n.id)}>{n.label}</button></li>
              ))}
            </ul>
          </>
        )}
      </div>
    );
  }
  const incoming = edges.filter((e) => e.target === node.id);
  const outgoing = edges.filter((e) => e.source === node.id);
  const metrics: [string, unknown][] = [
    ["Lines of code", node.loc], ["Loops", node.loop_count], ["DB operations", node.n_db_ops],
    ["DB operations in loops", node.n_db_ops_in_loop], ["External calls", node.n_external_calls],
    ["Calls per request (observed)", node.observed_calls_per_request],
  ];
  const conn = (list: DrawnEdge[], other: (e: DrawnEdge) => string, title: string) => (
    <>
      <div className="section-label">{title} ({list.length})</div>
      {list.length === 0 ? <p className="small muted">None</p> : (
        <ul className="plain small">
          {list.map((e) => (
            <li key={other(e)}>
              <button type="button" className="btn btn-link mono" onClick={() => onSelect(other(e))}>{label(other(e))}</button>{" "}
              <span className="muted">{e.kinds.join(", ")}{e.observed ? ", observed" : ""}{e.inLoop ? ", in loop" : ""}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
  return (
    <div className="detail" aria-live="polite">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <h3 className="mono" style={{ wordBreak: "break-all" }}>{node.label}</h3>
        <button type="button" className="btn btn-sm" onClick={() => onSelect(null)}>Clear</button>
      </div>
      <dl>
        <dt>Kind</dt><dd>{node.kind}{node.is_handler ? " (route handler)" : ""}</dd>
        {node.file && <><dt>Location</dt><dd className="mono">{node.file}:{node.line}</dd></>}
        {node.module && <><dt>Module</dt><dd className="mono">{node.module}</dd></>}
        {node.rules && node.rules.length > 0 && <><dt>Rules</dt><dd className="mono">{node.rules.join(", ")} <SeverityBadge severity={node.max_severity} /></dd></>}
        <dt>At runtime</dt><dd>{node.observed ? "Observed in the recorded runtime report" : "Not observed (or no runtime report used)"}</dd>
        {node.kind === "function" && metrics.filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => (
          <span key={k} style={{ display: "contents" }}><dt>{k}</dt><dd className="mono">{String(v)}</dd></span>
        ))}
      </dl>
      {conn(incoming, (e) => e.source, "Incoming")}
      {conn(outgoing, (e) => e.target, "Outgoing")}
    </div>
  );
}
