import { FileArchive, Play, Upload, X } from "lucide-react";
import { useId, useRef } from "react";
import { formatBytes } from "../format";
import type { AnalyzeRequest, ModelsResponse, ProjectsResponse, UploadConfig } from "../types";

export type Scope = "demo" | "all" | "upload";

export interface UploadPhase {
  phase: "idle" | "uploading" | "analysing";
  progress: number; // 0..1 of the upload itself
}

interface Props {
  projects: ProjectsResponse;
  models: ModelsResponse | null;
  form: AnalyzeRequest;
  scope: Scope;
  onScope: (s: Scope) => void;
  onChange: (f: AnalyzeRequest) => void;
  onRun: () => void;
  running: boolean;
  dirty: boolean;
  uploadConfig: UploadConfig | null;
  uploadFile: File | null;
  uploadFileError: string | null;
  onUploadFile: (file: File | null) => void;
  uploadPhase: UploadPhase;
  uploadedName: string | null;
  onDiscardUpload: () => void;
}

/** Client-side checks only; the server validates the archive again and is authoritative. */
export function checkZipFile(file: File, config: UploadConfig | null): string | null {
  if (!file.name.toLowerCase().endsWith(".zip")) return "Choose a .zip file.";
  if (file.size === 0) return "The file is empty.";
  if (config && file.size > config.max_upload_bytes) {
    return `The file is ${formatBytes(file.size)}; the limit is ${formatBytes(config.max_upload_bytes)}.`;
  }
  return null;
}

export function ProjectPanel(props: Props) {
  const { projects, models, form, scope, onScope, onChange, onRun, running, dirty } = props;
  const ids = { project: useId(), endpoint: useId(), model: useId(), runtime: useId(), file: useId() };
  const fileInput = useRef<HTMLInputElement>(null);
  const demos = projects.projects.filter((p) => p.demo);
  const options = scope === "demo" && demos.length ? demos : projects.projects;
  const selected = projects.projects.find((p) => p.path === form.project);
  const upload = scope === "upload";
  const { uploadConfig: config, uploadFile, uploadFileError, uploadPhase, uploadedName } = props;

  const runLabel = running
    ? uploadPhase.phase === "uploading"
      ? `Uploading ${Math.round(uploadPhase.progress * 100)}%`
      : "Analysing..."
    : upload && uploadFile
      ? "Upload and analyse"
      : "Run analysis";
  const canRun = !running && (!upload || (!!uploadFile && !uploadFileError) || (!!uploadedName && !uploadFile));

  return (
    <aside className="panel sidebar" aria-label="Analysis settings">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canRun) onRun();
        }}
      >
        <section>
          <div className="step-title">1. Project</div>
          <div className="segmented" role="group" aria-label="Project source">
            <button type="button" aria-pressed={scope === "demo"} onClick={() => onScope("demo")}>
              Demo
            </button>
            <button type="button" aria-pressed={scope === "all"} onClick={() => onScope("all")}>
              All folders
            </button>
            <button type="button" aria-pressed={upload} onClick={() => onScope("upload")}>
              Upload ZIP
            </button>
          </div>
          {!upload ? (
            <>
              <label className="field" htmlFor={ids.project}>
                Repository
              </label>
              <select id={ids.project} value={form.project} onChange={(e) => onChange({ ...form, project: e.target.value, endpoint: "" })}>
                {!options.some((p) => p.path === form.project) && <option value={form.project}>{form.project}</option>}
                {options.map((p) => (
                  <option key={p.path} value={p.path}>
                    {p.path}
                  </option>
                ))}
              </select>
              <p className="hint">
                Folders inside <span className="mono">{projects.allowed_root_name}</span> on the machine running the API. Code is
                parsed, never run.
              </p>
            </>
          ) : (
            <>
              <label className="field" htmlFor={ids.file}>
                ZIP archive of a FastAPI project
              </label>
              <input
                ref={fileInput}
                id={ids.file}
                className="file-input"
                type="file"
                accept=".zip,application/zip"
                aria-describedby={`${ids.file}-hint`}
                aria-invalid={!!uploadFileError}
                onChange={(e) => props.onUploadFile(e.target.files?.[0] ?? null)}
              />
              {uploadFile && (
                <div className="file-chip">
                  <FileArchive size={14} aria-hidden />
                  <span className="mono">{uploadFile.name}</span>
                  <span className="muted">{formatBytes(uploadFile.size)}</span>
                  <button
                    type="button"
                    className="btn btn-sm"
                    aria-label="Clear the chosen file"
                    onClick={() => {
                      props.onUploadFile(null);
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  >
                    <X size={12} aria-hidden />
                  </button>
                </div>
              )}
              {uploadFileError && (
                <p className="hint field-error" role="alert">
                  {uploadFileError}
                </p>
              )}
              <p className="hint" id={`${ids.file}-hint`}>
                {config
                  ? `Up to ${formatBytes(config.max_upload_bytes)}, ${config.max_members} entries, ${formatBytes(config.max_extracted_bytes)} of source. `
                  : ""}
                Python files are parsed, never run. The files are deleted right after analysis.
              </p>
              {uploadedName && !uploadFile && (
                <div className="file-chip">
                  <Upload size={14} aria-hidden />
                  <span>
                    Analysed: <span className="mono">{uploadedName}</span>
                  </span>
                  <button type="button" className="btn btn-sm" onClick={props.onDiscardUpload}>
                    Discard
                  </button>
                </div>
              )}
            </>
          )}
        </section>

        <section>
          <div className="step-title">2. Runtime evidence</div>
          {upload ? (
            <p className="hint" style={{ marginTop: 0 }}>
              Uploaded projects are analysed with static evidence only. Uploaded code is never run, and runtime reports inside the
              archive are ignored.
            </p>
          ) : selected?.runtime_report ? (
            <label className="checkbox" htmlFor={ids.runtime}>
              <input id={ids.runtime} type="checkbox" checked={form.use_runtime} onChange={(e) => onChange({ ...form, use_runtime: e.target.checked })} />
              <span>Use the recorded runtime report for this project</span>
            </label>
          ) : (
            <p className="hint" style={{ marginTop: 0 }}>
              No recorded runtime report for this project, so the analysis uses static evidence only.
            </p>
          )}
          {!upload && (
            <p className="hint">
              The web app never runs project code. Runtime reports are recorded separately with the CLI (
              <span className="mono">scaletrace ml collect-runtime --allow-execute</span>) on a trusted demo app.
            </p>
          )}
        </section>

        <section>
          <div className="step-title">3. Ranking</div>
          <div className="stack">
            <div>
              <label className="field" htmlFor={ids.endpoint}>
                Reported slow endpoint (optional)
              </label>
              <input
                id={ids.endpoint}
                type="text"
                placeholder="GET /orders/summary"
                value={form.endpoint}
                maxLength={200}
                onChange={(e) => onChange({ ...form, endpoint: e.target.value })}
              />
            </div>
            <div>
              <label className="field" htmlFor={ids.model}>
                Ranker
              </label>
              <select id={ids.model} value={form.model ?? ""} onChange={(e) => onChange({ ...form, model: e.target.value || null })} disabled={!models}>
                <option value="">Heuristic (no learned model)</option>
                {models?.models.map((m) => (
                  <option key={m} value={m}>
                    {m.startsWith("mlp") ? "MLP baseline" : "GNN"}: {m}
                  </option>
                ))}
              </select>
              <p className="hint">
                On the real static-only benchmark, no learned model beat the rules-only baseline. GNN scores order the
                candidates and are not probabilities.
              </p>
            </div>
          </div>
        </section>

        <section>
          <button type="submit" className="btn btn-primary btn-block" disabled={!canRun}>
            {upload && uploadFile ? <Upload size={14} aria-hidden /> : <Play size={14} aria-hidden />}
            {runLabel}
          </button>
          {running && uploadPhase.phase === "uploading" && (
            <div className="progress" role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(uploadPhase.progress * 100)}>
              <div style={{ width: `${uploadPhase.progress * 100}%` }} />
            </div>
          )}
          {upload && !uploadFile && !uploadedName && !running && (
            <p className="hint">Choose a ZIP file to analyse.</p>
          )}
          {dirty && !running && (
            <p className="hint" role="status">
              Settings changed. Run the analysis to update the results.
            </p>
          )}
        </section>
      </form>
    </aside>
  );
}
