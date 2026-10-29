import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { STATUS_HELP, STATUS_TEXT } from "../format";
import type { FindingStatus, Severity } from "../types";

export function SeverityBadge({ severity }: { severity: Severity | null | undefined }) {
  if (!severity) return null;
  return <span className={`badge sev-${severity}`}>{severity.toUpperCase()}</span>;
}

export function StatusBadge({ status }: { status: FindingStatus }) {
  return (
    <span className={`badge st-${status}`} title={STATUS_HELP[status]}>
      {STATUS_TEXT[status] ?? status}
    </span>
  );
}

type Tone = "info" | "error" | "ok" | "warn" | "neutral";
const ICONS: Record<Tone, ReactNode> = {
  info: <Info size={16} aria-hidden />,
  error: <XCircle size={16} aria-hidden />,
  ok: <CheckCircle2 size={16} aria-hidden />,
  warn: <AlertTriangle size={16} aria-hidden />,
  neutral: <Info size={16} aria-hidden />,
};

export function Notice({ tone = "neutral", children, role }: { tone?: Tone; children: ReactNode; role?: string }) {
  return (
    <div className={`notice ${tone === "neutral" ? "" : `notice-${tone}`}`} role={role ?? (tone === "error" ? "alert" : undefined)}>
      {ICONS[tone]}
      <div>{children}</div>
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden />
      {label}
    </div>
  );
}

export function PathView({ labels, resources = [] }: { labels: string[]; resources?: string[] }) {
  return (
    <span className="path">
      {labels.map((l, i) => (
        <span key={i} style={{ display: "contents" }}>
          {i > 0 && <span className="sep" aria-hidden>{"→"}</span>}
          <span className="node">{l}</span>
        </span>
      ))}
      {resources.length > 0 && (
        <>
          <span className="sep" aria-hidden>{"→"}</span>
          {resources.map((r) => (
            <span key={r} className="node resource">{r}</span>
          ))}
        </>
      )}
    </span>
  );
}

export function ScoreBar({ value }: { value: number | null }) {
  if (value === null) return <span className="muted">n/a</span>;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span className="bar" aria-hidden>
        <div style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </span>
      <span className="mono">{value.toFixed(2)}</span>
    </span>
  );
}
