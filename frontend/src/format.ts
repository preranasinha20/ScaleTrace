import type { Diagnosis, FindingStatus, Severity } from "./types";

export const STATUS_TEXT: Record<FindingStatus, string> = {
  static_candidate: "Static candidate",
  runtime_observed: "Seen at runtime only",
  correlated_finding: "Static and runtime agree",
  not_confirmed: "Not confirmed at runtime",
};

export const STATUS_HELP: Record<FindingStatus, string> = {
  static_candidate: "A code pattern matched a rule. No runtime measurement supports or contradicts it.",
  runtime_observed: "Growth was measured at runtime, but no static rule matched.",
  correlated_finding: "The rule matched and the recorded runtime measurements grow with input size as the rule predicts.",
  not_confirmed: "The rule matched, but the recorded workload did not show the predicted growth.",
};

export const DIAGNOSIS_TEXT: Record<Diagnosis["status"], string> = {
  correlated: "Static and runtime evidence agree",
  static_only: "Static evidence only",
  unexplained: "No code-level cause found",
};

export const SEVERITY_ORDER: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

export const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "n/a" : v.toFixed(digits));

export const humanCategory = (c: string) => c.replace(/_/g, " ");

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
