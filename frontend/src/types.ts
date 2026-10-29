// Response shapes of the ScaleTrace API (scaletrace/api/server.py). Mirrors the Python pydantic schemas.

export type Severity = "high" | "medium" | "low";
export type FindingStatus = "static_candidate" | "runtime_observed" | "correlated_finding" | "not_confirmed";

export interface Project {
  path: string;
  demo: boolean;
  runtime_report: boolean;
  workload_file: boolean;
}

export interface ProjectsResponse {
  allowed_root_name: string;
  default: string | null;
  projects: Project[];
}

export interface ModelsResponse {
  models: string[];
  note: string;
}

export interface StaticEvidence {
  pattern: string;
  description: string;
  snippet: string;
  details: Record<string, unknown>;
}

export interface RuntimeEvidence {
  metric: string;
  value: unknown;
  workload: string | null;
  description: string;
}

export interface Finding {
  finding_id: string;
  rule_id: string;
  rule_name: string;
  category: string;
  severity: Severity;
  classification: string;
  file: string;
  line: number;
  end_line: number | null;
  column: number | null;
  function: string;
  endpoint: string | null;
  reachable_endpoints: string[];
  message: string;
  static_evidence: StaticEvidence[];
  runtime_evidence: RuntimeEvidence[];
  status: FindingStatus;
  possible_false_positives: string[];
  verification_steps: string[];
}

export interface Symptom {
  symptom_id: string;
  kind: "query_growth" | "latency_growth" | "memory_growth";
  endpoint: string | null;
  resource: string | null;
  sizes: number[];
  values: (number | null)[];
  unit: string;
  growth_exponent: number | null;
  growth_class: string;
  description: string;
}

export interface Candidate {
  node: string;
  label: string;
  file: string | null;
  line: number | null;
  static_score: number | null;
  dynamic_score: number | null;
  importance: number | null;
  risk: number;
  missing_terms: string[];
  findings: string[];
  rules: string[];
  gnn_score: number | null;
}

export interface DiagnosticStep {
  order: number;
  text: string;
  evidence: "runtime" | "static" | "graph" | "inference" | "learned";
}

export interface Diagnosis {
  diagnosis_id: string;
  symptom: Symptom;
  supporting_symptoms: Symptom[];
  start_node: string;
  status: "correlated" | "static_only" | "unexplained";
  approximate: boolean;
  ranking_method: string;
  path: string[];
  path_labels: string[];
  candidates: Candidate[];
  probable_cause: string;
  steps: DiagnosticStep[];
  related_findings: string[];
  verification_steps: string[];
  explanation: Record<string, unknown> | null;
}

export interface RiskPath {
  endpoint: string;
  nodes: string[];
  labels: string[];
  rules: string[];
  max_severity: Severity;
  findings: string[];
  resources: string[];
}

export interface RuntimeStatus {
  recorded_report_available: boolean;
  used: boolean;
  report_status: string | null;
  workload_file_present: boolean;
  executes_code_from_api: boolean;
  generated_at: string | null;
}

export interface AnalyzeRequest {
  project: string;
  use_runtime: boolean;
  endpoint: string;
  model: string | null;
}

export interface Analysis {
  project: string;
  repository: string;
  scaletrace_version: string;
  generated_at: string;
  request: AnalyzeRequest;
  ranking: string;
  runtime: RuntimeStatus;
  summary: {
    files_analyzed: number;
    files_skipped: number;
    findings: number;
    findings_by_status: Partial<Record<FindingStatus, number>>;
    findings_by_severity: Record<Severity, number>;
    diagnoses: number;
    rules_applied: string[];
    rules_not_implemented: string[];
    rulebook_version: string;
    rulebook_status: string;
  };
  findings: Finding[];
  risk_paths: RiskPath[];
  diagnoses: Diagnosis[];
  unexplained_observations: { symptom: Symptom; status: string; note: string }[];
  scoring: Record<string, unknown>;
  limitations: string[];
  upload?: UploadInfo;
}

export interface GraphNode {
  id: string;
  kind: "endpoint" | "function" | "resource";
  label: string;
  level: number;
  relevant: boolean;
  file?: string;
  line?: number;
  module?: string;
  findings?: string[];
  rules?: string[];
  max_severity?: Severity | null;
  observed?: boolean;
  is_handler?: boolean;
  [key: string]: unknown;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: string;
  count?: number;
  in_loop?: boolean;
  lines?: number[];
  static?: boolean;
  observed?: boolean;
  observed_calls_per_request?: number | null;
}

export interface GraphData {
  graph_format_version: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface UploadConfig {
  max_upload_bytes: number;
  max_extracted_bytes: number;
  max_file_bytes: number;
  max_members: number;
  max_depth: number;
  ttl_seconds: number;
}

export interface UploadInfo {
  upload_id: string;
  filename: string;
  name: string;
  project_root: string;
  python_files: number;
  files_extracted: number;
  bytes_extracted: number;
  members_skipped: number;
  static_only: boolean;
  expires_in_seconds: number;
}

export interface SourceContext {
  file: string;
  start: number;
  highlight: number;
  lines: string[];
}
