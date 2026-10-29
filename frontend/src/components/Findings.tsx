import { ArrowDown, ArrowUp } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { SEVERITY_ORDER, STATUS_HELP, STATUS_TEXT, humanCategory } from "../format";
import type { Finding, FindingStatus, SourceContext } from "../types";
import { Loading, Notice, SeverityBadge, StatusBadge } from "./ui";

type SortKey = "severity" | "rule" | "location" | "status";

const compare: Record<SortKey, (a: Finding, b: Finding) => number> = {
  severity: (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
  rule: (a, b) => a.rule_id.localeCompare(b.rule_id),
  location: (a, b) => a.file.localeCompare(b.file) || a.line - b.line,
  status: (a, b) => a.status.localeCompare(b.status),
};

export type SourceLoader = (file: string, line: number) => Promise<SourceContext>;

export function Findings({ loadSource, findings }: { loadSource: SourceLoader; findings: Finding[] }) {
  const [query, setQuery] = useState("");
  const [rule, setRule] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({ key: "severity", asc: true });
  const [selectedId, setSelectedId] = useState<string | null>(findings[0]?.finding_id ?? null);

  const rules = useMemo(() => [...new Set(findings.map((f) => f.rule_id))].sort(), [findings]);
  const statuses = useMemo(() => [...new Set(findings.map((f) => f.status))].sort(), [findings]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = findings.filter(
      (f) =>
        (!rule || f.rule_id === rule) &&
        (!severity || f.severity === severity) &&
        (!status || f.status === status) &&
        (!q ||
          [f.rule_id, f.rule_name, f.file, f.function, f.endpoint ?? "", f.message].some((s) => s.toLowerCase().includes(q))),
    );
    const cmp = compare[sort.key];
    return [...rows].sort((a, b) => (sort.asc ? 1 : -1) * (cmp(a, b) || compare.location(a, b)));
  }, [findings, query, rule, severity, status, sort]);

  const selected = findings.find((f) => f.finding_id === selectedId) ?? null;

  if (findings.length === 0) return <Notice tone="ok">No findings in this project for the rules applied.</Notice>;

  const header = (key: SortKey, label: string) => (
    <th scope="col" aria-sort={sort.key === key ? (sort.asc ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => setSort((s) => ({ key, asc: s.key === key ? !s.asc : true }))}>
        {label}
        {sort.key === key && (sort.asc ? <ArrowUp size={12} aria-hidden /> : <ArrowDown size={12} aria-hidden />)}
      </button>
    </th>
  );

  return (
    <div>
      <p className="muted small">
        Findings are code patterns that match a rulebook rule. They are candidates, not proven defects, unless the status
        says runtime evidence agrees.
      </p>
      <div className="toolbar" role="search">
        <label className="sr-only" htmlFor="finding-search">Search findings</label>
        <input id="finding-search" type="search" placeholder="Search rule, file, function, endpoint" value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="sr-only" htmlFor="f-rule">Rule</label>
        <select id="f-rule" value={rule} onChange={(e) => setRule(e.target.value)}>
          <option value="">All rules</option>
          {rules.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <label className="sr-only" htmlFor="f-sev">Severity</label>
        <select id="f-sev" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">All severities</option>
          {(["high", "medium", "low"] as const).filter((s) => findings.some((f) => f.severity === s)).map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <label className="sr-only" htmlFor="f-status">Evidence status</label>
        <select id="f-status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {statuses.map((s) => <option key={s} value={s}>{STATUS_TEXT[s as FindingStatus]}</option>)}
        </select>
        <span className="small muted" aria-live="polite">{shown.length} of {findings.length}</span>
      </div>

      <div className="split">
        <div className="table-wrap">
          <table aria-label="Findings">
            <thead>
              <tr>
                {header("rule", "Rule")}
                {header("severity", "Severity")}
                {header("location", "Location and function")}
                {header("status", "Evidence")}
              </tr>
            </thead>
            <tbody>
              {shown.map((f) => (
                <tr
                  key={f.finding_id}
                  className="selectable"
                  aria-selected={f.finding_id === selectedId}
                  tabIndex={0}
                  onClick={() => setSelectedId(f.finding_id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedId(f.finding_id);
                    }
                  }}
                >
                  <td className="mono">{f.rule_id}</td>
                  <td><SeverityBadge severity={f.severity} /></td>
                  <td>
                    <div className="mono">{f.file}:{f.line}</div>
                    <div className="mono muted small">{f.function}</div>
                  </td>
                  <td><StatusBadge status={f.status} /></td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr><td colSpan={4} className="empty">No findings match these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {selected ? <FindingDetail loadSource={loadSource} finding={selected} /> : <div className="detail muted">Select a finding to see its evidence.</div>}
      </div>
    </div>
  );
}

function FindingDetail({ loadSource, finding: f }: { loadSource: SourceLoader; finding: Finding }) {
  const [source, setSource] = useState<SourceContext | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setSource(null);
    setError(null);
    loadSource(f.file, f.line).then(
      (s) => live && setSource(s),
      (e: Error) => live && setError(e.message),
    );
    return () => {
      live = false;
    };
  }, [loadSource, f.file, f.line]);

  return (
    <article className="detail" aria-label={`Finding ${f.rule_id} details`}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
        <SeverityBadge severity={f.severity} />
        <StatusBadge status={f.status} />
      </div>
      <h3>{f.rule_id}: {f.rule_name}</h3>
      <dl>
        <dt>Category</dt><dd>{humanCategory(f.category)} ({f.classification})</dd>
        <dt>Location</dt><dd className="mono">{f.file}:{f.line}{f.end_line && f.end_line !== f.line ? `-${f.end_line}` : ""}</dd>
        <dt>Function</dt><dd className="mono">{f.function}</dd>
        <dt>Endpoints</dt><dd className="mono">{f.reachable_endpoints.length ? f.reachable_endpoints.join(", ") : "none found"}</dd>
        <dt>Evidence</dt><dd>{STATUS_HELP[f.status]}</dd>
      </dl>

      <div className="section-label">Static evidence</div>
      {f.static_evidence.map((ev, i) => (
        <div key={i}>
          <p className="small">{ev.description}</p>
          <pre className="snippet mono">{ev.snippet}</pre>
        </div>
      ))}

      <div className="section-label">Source context</div>
      {error && <Notice tone="warn">Source not available: {error}</Notice>}
      {!error && !source && <Loading label="Loading source" />}
      {source && (
        <div className="code" aria-label={`Source lines ${source.start} to ${source.start + source.lines.length - 1}`}>
          <pre>
            {source.lines.map((text, i) => {
              const n = source.start + i;
              return (
                <div key={n} className={`ln${n === source.highlight ? " hl" : ""}`}>
                  <span>{n}</span>
                  <span>{text || " "}</span>
                </div>
              );
            })}
          </pre>
        </div>
      )}

      <div className="section-label">Runtime evidence</div>
      {f.runtime_evidence.length === 0 ? (
        <p className="small muted">None recorded for this finding.</p>
      ) : (
        <ul className="plain small">
          {f.runtime_evidence.map((ev, i) => (
            <li key={i}>
              <span className="mono">{ev.metric}</span>: {ev.description || String(ev.value)}
            </li>
          ))}
        </ul>
      )}

      {f.possible_false_positives.length > 0 && (
        <>
          <div className="section-label">Could be a false positive if</div>
          <ul className="plain small">{f.possible_false_positives.map((x) => <li key={x}>{x}</li>)}</ul>
        </>
      )}
      {f.verification_steps.length > 0 && (
        <>
          <div className="section-label">How to verify</div>
          <ul className="plain small">{f.verification_steps.map((x) => <li key={x}>{x}</li>)}</ul>
        </>
      )}
    </article>
  );
}
