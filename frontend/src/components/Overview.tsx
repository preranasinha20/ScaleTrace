import { DIAGNOSIS_TEXT } from "../format";
import type { Analysis } from "../types";
import { Notice, PathView, SeverityBadge } from "./ui";

export function Overview({ analysis, onOpenDiagnoses }: { analysis: Analysis; onOpenDiagnoses: () => void }) {
  const { risk_paths: paths, diagnoses, summary, runtime } = analysis;
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div>
        <h2>Risk paths</h2>
        <p className="muted small">
          Endpoint, then the code it reaches, then the resource. Each path ends at a function with at least one rule match.
        </p>
        {paths.length === 0 ? (
          <Notice tone="ok">No risk paths: no endpoint reaches code that matches a scalability rule.</Notice>
        ) : (
          <div className="list-rows">
            {paths.map((p, i) => (
              <div key={i} style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                <SeverityBadge severity={p.max_severity} />
                <PathView labels={p.labels} resources={p.resources} />
                <span className="mono small muted">[{p.rules.join(", ")}]</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <h2>Most likely root causes</h2>
        {diagnoses.length === 0 ? (
          <p className="muted">
            No symptoms to diagnose.{" "}
            {runtime.used
              ? "The recorded runtime report shows no growing endpoint."
              : "Use a recorded runtime report or enter a reported slow endpoint, then run the analysis."}
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Symptom</th>
                  <th scope="col">Top candidate</th>
                  <th scope="col">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {diagnoses.map((d) => (
                  <tr key={d.diagnosis_id}>
                    <td className="mono">{d.symptom.endpoint ?? d.symptom.description}</td>
                    <td className="mono">{d.candidates[0]?.label ?? "none"}</td>
                    <td>{DIAGNOSIS_TEXT[d.status] ?? d.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {diagnoses.length > 0 && (
          <button type="button" className="btn btn-sm" style={{ marginTop: 8 }} onClick={onOpenDiagnoses}>
            View ranked candidates
          </button>
        )}
      </div>

      <div>
        <h2>Scope of this analysis</h2>
        <ul className="plain small" style={{ marginTop: 6 }}>
          <li>
            Rules applied ({summary.rules_applied.length}): <span className="mono">{summary.rules_applied.join(", ")}</span>
          </li>
          <li>
            Rulebook {summary.rulebook_version} ({summary.rulebook_status}). Rules without a detector yet:{" "}
            <span className="mono">{summary.rules_not_implemented.join(", ") || "none"}</span>
          </li>
          {summary.files_skipped > 0 && <li>{summary.files_skipped} files were skipped (unparseable or excluded).</li>}
          {analysis.limitations.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
