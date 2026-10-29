import { DIAGNOSIS_TEXT, fmt } from "../format";
import type { Analysis, Diagnosis, Symptom } from "../types";
import { Notice, PathView, ScoreBar } from "./ui";

const KIND_TITLE: Record<Symptom["kind"], string> = {
  query_growth: "SQL queries per request",
  latency_growth: "Latency per request",
  memory_growth: "Memory growth",
};

/** Measured values from the recorded runtime report, plotted against input size. One series per chart. */
function GrowthChart({ symptom }: { symptom: Symptom }) {
  const points = symptom.sizes
    .map((size, i) => ({ size, value: symptom.values[i] }))
    .filter((p): p is { size: number; value: number } => typeof p.value === "number");
  if (points.length < 2) return null;
  const W = 280, H = 140, L = 44, R = 14, T = 12, B = 30;
  const maxV = Math.max(...points.map((p) => p.value)) || 1;
  const logX = points.every((p) => p.size > 0) && points[points.length - 1].size / points[0].size >= 20;
  const xs = points.map((p) => (logX ? Math.log10(p.size) : p.size));
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const sx = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const sy = (v: number) => H - B - (v / maxV) * (H - T - B);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(1)},${sy(p.value).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  return (
    <figure className="chart-card" style={{ margin: 0 }}>
      <figcaption className="small" style={{ fontWeight: 600, color: "var(--gray-700)" }}>
        {KIND_TITLE[symptom.kind]} <span className="muted">({symptom.unit})</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: 360, display: "block" }} role="img" aria-label={symptom.description}>
        <line x1={L} x2={W - R} y1={H - B} y2={H - B} stroke="#d1d5db" />
        <line x1={L} x2={W - R} y1={sy(maxV)} y2={sy(maxV)} stroke="#f3f4f6" />
        <text x={L - 6} y={sy(maxV) + 3} textAnchor="end">{Number(maxV.toPrecision(3))}</text>
        <text x={L - 6} y={H - B + 3} textAnchor="end">0</text>
        {points.map((p, i) => (
          <text key={p.size} x={sx(xs[i])} y={H - B + 14} textAnchor="middle">{p.size}</text>
        ))}
        <text x={(L + W - R) / 2} y={H - 3} textAnchor="middle">input size{logX ? " (log scale)" : ""}</text>
        <path d={d} fill="none" stroke="#0f766e" strokeWidth={2} />
        {points.map((p, i) => (
          <g key={p.size}>
            <circle cx={sx(xs[i])} cy={sy(p.value)} r={10} fill="transparent">
              <title>{`size ${p.size}: ${Number(p.value.toPrecision(4))} ${symptom.unit}`}</title>
            </circle>
            <circle cx={sx(xs[i])} cy={sy(p.value)} r={4} fill="#0f766e" stroke="#fff" strokeWidth={2} pointerEvents="none" />
          </g>
        ))}
        <text x={sx(xs[xs.length - 1]) - 6} y={sy(last.value) - 8} textAnchor="end" style={{ fill: "#111827" }}>
          {Number(last.value.toPrecision(3))}
        </text>
      </svg>
      <p className="small muted" style={{ margin: 0 }}>
        Growth: {symptom.growth_class}
        {symptom.growth_exponent !== null && `, exponent ${symptom.growth_exponent.toFixed(2)}`}
      </p>
    </figure>
  );
}

function RankingBasis({ diagnosis, scoring }: { diagnosis: Diagnosis; scoring: Record<string, unknown> }) {
  const w = (k: string) => (typeof scoring[k] === "number" ? (scoring[k] as number) : null);
  const learned = diagnosis.candidates.some((c) => c.gnn_score !== null);
  return (
    <p className="small muted">
      {learned
        ? "Ordered by the learned model score, then by heuristic risk. The learned score is a ranking score, not a probability. "
        : "Ordered by heuristic risk. "}
      Risk = ({fmt(w("w_static"), 1)} x static + {fmt(w("w_dynamic"), 1)} x runtime + {fmt(w("w_importance"), 1)} x
      graph importance), divided by the sum of the weights of the terms that are available. Each term is normalised to 0 to 1.
      Missing terms are excluded, not counted as zero.
    </p>
  );
}

export function RootCauses({ analysis }: { analysis: Analysis }) {
  const { diagnoses, unexplained_observations: unexplained, runtime } = analysis;
  if (diagnoses.length === 0 && unexplained.length === 0) {
    return (
      <Notice tone="info">
        No symptoms to diagnose.{" "}
        {runtime.recorded_report_available
          ? "Enable the recorded runtime report, or enter a reported slow endpoint, and run the analysis."
          : "This project has no recorded runtime report. Enter a reported slow endpoint (for example GET /orders) and run the analysis."}
      </Notice>
    );
  }
  return (
    <div>
      {diagnoses.map((d) => (
        <section key={d.diagnosis_id} className="diag panel panel-body" aria-label={`Diagnosis for ${d.symptom.endpoint ?? "symptom"}`}>
          <div className="diag-head">
            <h2 className="mono">{d.symptom.endpoint ?? "Symptom"}</h2>
            <span className="small">
              <strong>{DIAGNOSIS_TEXT[d.status]}</strong>
              <span className="muted"> · ranking: {d.ranking_method}{d.approximate ? " · approximate (runtime attribution incomplete)" : ""}</span>
            </span>
          </div>
          <p style={{ marginTop: 6 }}>{d.symptom.description}</p>
          {[d.symptom, ...d.supporting_symptoms].some((s) => s.sizes.length > 1) && (
            <div className="cards">
              {[d.symptom, ...d.supporting_symptoms].map((s) => <GrowthChart key={s.symptom_id} symptom={s} />)}
            </div>
          )}
          {d.supporting_symptoms.map((s) => (
            <p key={s.symptom_id} className="small muted">Also observed: {s.description}</p>
          ))}
          <div className="section-label">Path traced in the knowledge graph</div>
          <PathView labels={d.path_labels} />
          <p style={{ marginTop: 10, fontWeight: 600 }}>{d.probable_cause}</p>

          <div className="section-label">Ranked candidates</div>
          <RankingBasis diagnosis={d} scoring={analysis.scoring} />
          {d.candidates.length === 0 ? (
            <p className="muted">No candidate functions were found on the path.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col" className="num">Rank</th>
                    <th scope="col">Function</th>
                    <th scope="col">Location</th>
                    {d.candidates.some((c) => c.gnn_score !== null) && <th scope="col">Learned score</th>}
                    <th scope="col">Risk</th>
                    <th scope="col" className="num">Static</th>
                    <th scope="col" className="num">Runtime</th>
                    <th scope="col" className="num">Importance</th>
                    <th scope="col">Rules</th>
                  </tr>
                </thead>
                <tbody>
                  {d.candidates.map((c, i) => (
                    <tr key={c.node}>
                      <td className="num">{i + 1}</td>
                      <td className="mono">{c.label}</td>
                      <td className="mono">{c.file ? `${c.file}:${c.line}` : ""}</td>
                      {d.candidates.some((x) => x.gnn_score !== null) && <td><ScoreBar value={c.gnn_score} /></td>}
                      <td><ScoreBar value={c.risk} /></td>
                      <td className="num mono">{fmt(c.static_score)}</td>
                      <td className="num mono" title={c.missing_terms.includes("D") ? "No runtime measurement for this function" : undefined}>
                        {fmt(c.dynamic_score)}
                      </td>
                      <td className="num mono">{fmt(c.importance)}</td>
                      <td className="mono">{c.rules.join(", ") || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <details style={{ marginTop: 12 }}>
            <summary>How this diagnosis was reached ({d.steps.length} steps)</summary>
            <ol className="steps">
              {d.steps.map((s) => (
                <li key={s.order}>
                  <span className={`badge ev-${s.evidence}`}>{s.evidence}</span> {s.text}
                </li>
              ))}
            </ol>
          </details>
          {d.verification_steps.length > 0 && (
            <details>
              <summary>How to verify</summary>
              <ul className="plain small">{d.verification_steps.map((v) => <li key={v}>{v}</li>)}</ul>
            </details>
          )}
          {d.explanation && (
            <details>
              <summary>Learned-model explanation (raw)</summary>
              <pre className="snippet mono">{JSON.stringify(d.explanation, null, 2)}</pre>
            </details>
          )}
        </section>
      ))}
      {unexplained.length > 0 && (
        <section className="diag">
          <h2>Observed at runtime without a code-level cause</h2>
          <ul className="plain">
            {unexplained.map((o) => <li key={o.symptom.symptom_id}>{o.symptom.description} <span className="muted">{o.note}</span></li>)}
          </ul>
        </section>
      )}
    </div>
  );
}
