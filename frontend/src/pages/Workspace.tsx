import { FileSearch, GitBranch, LayoutDashboard, ListTree, Share2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Exports } from "../components/Exports";
import { Findings, type SourceLoader } from "../components/Findings";
import { GraphView } from "../components/GraphView";
import { Overview } from "../components/Overview";
import { checkZipFile, ProjectPanel, type Scope, type UploadPhase } from "../components/ProjectPanel";
import { RootCauses } from "../components/RootCauses";
import { Loading, Notice } from "../components/ui";
import { formatBytes, formatDate } from "../format";
import type { Analysis, AnalyzeRequest, GraphData, ModelsResponse, ProjectsResponse, UploadConfig } from "../types";

type Tab = "overview" | "findings" | "causes" | "graph" | "export";

/** Where the results shown come from: a folder on the server, or an uploaded ZIP kept in server memory. */
type Source =
  | { kind: "project"; request: AnalyzeRequest }
  | { kind: "upload"; id: string; name: string; endpoint: string; model: string | null };

const sameSource = (a: Source | null, b: Source | null) => {
  if (!a || !b || a.kind !== b.kind) return false;
  if (a.kind === "project" && b.kind === "project") {
    const x = a.request, y = b.request;
    return x.project === y.project && x.use_runtime === y.use_runtime && x.endpoint.trim() === y.endpoint.trim() && x.model === y.model;
  }
  return a.kind === "upload" && b.kind === "upload" && a.id === b.id && a.endpoint.trim() === b.endpoint.trim() && a.model === b.model;
};

function loadGraph(source: Source): Promise<GraphData> {
  return source.kind === "project" ? api.graph(source.request) : api.uploadGraph(source.id, source.endpoint, source.model);
}

export function Workspace() {
  const [projects, setProjects] = useState<ProjectsResponse | null>(null);
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [uploadConfig, setUploadConfig] = useState<UploadConfig | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [scope, setScope] = useState<Scope>("demo");
  const [form, setForm] = useState<AnalyzeRequest>({ project: "", use_runtime: true, endpoint: "", model: null });
  const [applied, setApplied] = useState<Source | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [graph, setGraph] = useState<{ key: Source; data: GraphData } | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadFileError, setUploadFileError] = useState<string | null>(null);
  const [uploadPhase, setUploadPhase] = useState<UploadPhase>({ phase: "idle", progress: 0 });
  const [lastUpload, setLastUpload] = useState<{ id: string; name: string } | null>(null);
  const seq = useRef(0);

  const show = useCallback((result: Analysis, source: Source) => {
    setAnalysis(result);
    setApplied(source);
    setGraph(null);
    setGraphError(null);
  }, []);

  const runProject = useCallback(async (req: AnalyzeRequest) => {
    const id = ++seq.current;
    setRunning(true);
    setError(null);
    try {
      const result = await api.analyze(req);
      if (id === seq.current) show(result, { kind: "project", request: req });
    } catch (e) {
      if (id === seq.current) setError((e as Error).message);
    } finally {
      if (id === seq.current) setRunning(false);
    }
  }, [show]);

  const runUpload = useCallback(async () => {
    const id = ++seq.current;
    setRunning(true);
    setError(null);
    try {
      let result: Analysis;
      if (uploadFile) {
        setUploadPhase({ phase: "uploading", progress: 0 });
        result = await api.upload(
          uploadFile, form.endpoint, form.model,
          (progress) => setUploadPhase({ phase: "uploading", progress }),
          () => setUploadPhase({ phase: "analysing", progress: 1 }),
        );
      } else if (lastUpload) {
        setUploadPhase({ phase: "analysing", progress: 1 });
        result = await api.uploadAnalyze(lastUpload.id, form.endpoint, form.model);
      } else {
        throw new Error("Choose a ZIP file to analyse.");
      }
      if (id !== seq.current) return;
      const info = result.upload!;
      setLastUpload({ id: info.upload_id, name: info.name });
      setUploadFile(null);
      show(result, { kind: "upload", id: info.upload_id, name: info.name, endpoint: form.endpoint, model: form.model });
    } catch (e) {
      if (id !== seq.current) return;
      const message = (e as Error).message;
      setError(message);
      if (!uploadFile && lastUpload && /not available/.test(message)) setLastUpload(null);
    } finally {
      if (id === seq.current) {
        setRunning(false);
        setUploadPhase({ phase: "idle", progress: 0 });
      }
    }
  }, [uploadFile, lastUpload, form.endpoint, form.model, show]);

  const loadProjects = useCallback(() => {
    setBootError(null);
    api.projects().then(
      (p) => {
        setProjects(p);
        if (p.default) {
          const initial = { project: p.default, use_runtime: true, endpoint: "", model: null };
          setForm(initial);
          void runProject(initial);
        }
      },
      (e: Error) => setBootError(e.message),
    );
    api.models().then(setModels, () => setModels({ models: [], note: "" }));
    api.uploadConfig().then(setUploadConfig, () => setUploadConfig(null));
  }, [runProject]);

  useEffect(loadProjects, [loadProjects]);

  useEffect(() => {
    if (tab !== "graph" || !applied || (graph && sameSource(graph.key, applied))) return;
    let live = true;
    setGraphError(null);
    loadGraph(applied).then(
      (data) => live && setGraph({ key: applied, data }),
      (e: Error) => live && setGraphError(e.message),
    );
    return () => {
      live = false;
    };
  }, [tab, applied, graph]);

  const loadSource: SourceLoader = useMemo(() => {
    if (applied?.kind === "upload") {
      const id = applied.id;
      return (file, line) => api.uploadSource(id, file, line);
    }
    const project = applied?.kind === "project" ? applied.request.project : "";
    return (file, line) => api.source(project, file, line);
  }, [applied]);

  const onUploadFile = (file: File | null) => {
    setUploadFile(file);
    setUploadFileError(file ? checkZipFile(file, uploadConfig) : null);
  };

  const discardUpload = () => {
    if (!lastUpload) return;
    const id = lastUpload.id;
    setLastUpload(null);
    api.uploadDelete(id).catch(() => undefined);
    if (applied?.kind === "upload" && applied.id === id) {
      setAnalysis(null);
      setApplied(null);
      setGraph(null);
    }
  };

  if (bootError) {
    return (
      <div className="panel panel-body">
        <Notice tone="error">{bootError}</Notice>
        <button type="button" className="btn" style={{ marginTop: 12 }} onClick={loadProjects}>Retry</button>
      </div>
    );
  }
  if (!projects) return <Loading label="Connecting to the ScaleTrace API" />;

  const currentSource: Source | null =
    scope === "upload"
      ? lastUpload ? { kind: "upload", id: lastUpload.id, name: lastUpload.name, endpoint: form.endpoint, model: form.model } : null
      : { kind: "project", request: form };
  const dirty = !!applied && ((scope === "upload" && !!uploadFile) || !sameSource(applied, currentSource));

  const tabs: { id: Tab; label: string; icon: JSX.Element; count?: number }[] = [
    { id: "overview", label: "Overview", icon: <LayoutDashboard size={14} aria-hidden /> },
    { id: "findings", label: "Findings", icon: <FileSearch size={14} aria-hidden />, count: analysis?.summary.findings },
    { id: "causes", label: "Root causes", icon: <ListTree size={14} aria-hidden />, count: analysis?.summary.diagnoses },
    { id: "graph", label: "Knowledge graph", icon: <GitBranch size={14} aria-hidden /> },
    { id: "export", label: "Export", icon: <Share2 size={14} aria-hidden /> },
  ];
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
    if (next === null) return;
    const t = tabs[(next + tabs.length) % tabs.length];
    setTab(t.id);
    document.getElementById(`tab-${t.id}`)?.focus();
  };

  const exportDownload = (format: string) => {
    if (applied?.kind === "upload") return api.uploadDownload(applied.id, format, applied.endpoint, applied.model);
    if (applied?.kind === "project") return api.download(format, applied.request);
    return Promise.reject(new Error("Nothing has been analysed yet."));
  };
  const exportDescription = (() => {
    if (!applied) return "";
    const ranking = (model: string | null) => (model ? `learned model ${model}` : "heuristic ranking");
    if (applied.kind === "upload") return `uploaded archive ${applied.name}, static only, ${ranking(applied.model)}`;
    const r = applied.request;
    return `${r.project}, ${r.use_runtime ? "recorded runtime report if available" : "static only"}, ${ranking(r.model)}`;
  })();

  const upload = analysis?.upload;
  return (
    <div className="workspace">
      <ProjectPanel
        projects={projects}
        models={models}
        form={form}
        scope={scope}
        onScope={setScope}
        onChange={setForm}
        onRun={() => (scope === "upload" ? runUpload() : runProject(form))}
        running={running}
        dirty={dirty}
        uploadConfig={uploadConfig}
        uploadFile={uploadFile}
        uploadFileError={uploadFileError}
        onUploadFile={onUploadFile}
        uploadPhase={uploadPhase}
        uploadedName={lastUpload?.name ?? null}
        onDiscardUpload={discardUpload}
      />
      <div style={{ minWidth: 0 }}>
        <div className="intro">
          <h1>Scalability diagnosis</h1>
          <p>
            ScaleTrace parses a FastAPI backend, finds code patterns that may slow down or use more memory as data grows, links them
            to the endpoints they affect, and ranks likely root causes. Static results are candidates; runtime evidence, when a
            recorded report exists, shows whether the predicted growth was measured.
          </p>
        </div>
        {error && <div style={{ marginBottom: 12 }}><Notice tone="error">{applied?.kind === "upload" || scope === "upload" ? "Upload failed" : "Analysis failed"}: {error}</Notice></div>}
        {!analysis && running && (
          <Loading label={uploadPhase.phase === "uploading" ? "Uploading the archive" : "Analysing the code (parsing only, nothing is executed)"} />
        )}
        {!analysis && !running && scope === "upload" && !error && (
          <Notice tone="info">
            Choose a ZIP of a FastAPI project and select Upload and analyse. Python files and common config files are extracted into a
            private temporary folder, parsed, and deleted right after the analysis. Nothing in the archive is run or installed.
          </Notice>
        )}
        {analysis && (
          <>
            <div className="summary-bar" aria-live="polite">
              <span>Project <strong className="mono">{upload ? upload.filename : analysis.project}</strong></span>
              {upload && (
                <span>Root <strong className="mono">{upload.project_root || "archive top level"}</strong></span>
              )}
              <span>Evidence <strong>{analysis.runtime.used ? "static + recorded runtime" : "static only"}</strong></span>
              {analysis.runtime.used && <span>Runtime report <strong>{formatDate(analysis.runtime.generated_at)}</strong></span>}
              <span>Ranking <strong>{analysis.ranking}</strong></span>
              <span>Analysed <strong>{formatDate(analysis.generated_at)}</strong></span>
              {running && <span className="loading" style={{ padding: 0 }}><span className="spinner" aria-hidden />Updating</span>}
            </div>
            {upload && (
              <div style={{ marginBottom: 12 }}>
                <Notice tone="neutral">
                  Extracted {upload.files_extracted} files ({formatBytes(upload.bytes_extracted)}, {upload.python_files} Python) and
                  skipped {upload.members_skipped} other entries. The extracted files were deleted after parsing; results stay in server
                  memory for about {Math.max(1, Math.round(upload.expires_in_seconds / 60))} minutes or until you discard them.
                </Notice>
              </div>
            )}
            <div className="metrics">
              <div className="panel metric">
                <div className="label">Python files analysed</div>
                <div className="value">{analysis.summary.files_analyzed}</div>
                <div className="sub">{analysis.summary.files_skipped} skipped</div>
              </div>
              <div className="panel metric">
                <div className="label">Findings</div>
                <div className="value">{analysis.summary.findings}</div>
                <div className="sub">
                  {analysis.summary.findings_by_severity.high} high, {analysis.summary.findings_by_severity.medium} medium, {analysis.summary.findings_by_severity.low} low
                </div>
              </div>
              <div className="panel metric">
                <div className="label">Confirmed by runtime evidence</div>
                <div className="value">{analysis.summary.findings_by_status.correlated_finding ?? 0}</div>
                <div className="sub">{analysis.runtime.used ? `${analysis.summary.findings_by_status.not_confirmed ?? 0} not confirmed` : "no runtime report used"}</div>
              </div>
              <div className="panel metric">
                <div className="label">Diagnoses</div>
                <div className="value">{analysis.summary.diagnoses}</div>
                <div className="sub">slow or growing endpoints traced</div>
              </div>
            </div>

            <div className="panel">
              <div className="tabs" role="tablist" aria-label="Results">
                {tabs.map((t, i) => (
                  <button
                    key={t.id}
                    id={`tab-${t.id}`}
                    role="tab"
                    type="button"
                    aria-selected={tab === t.id}
                    aria-controls={`panel-${t.id}`}
                    tabIndex={tab === t.id ? 0 : -1}
                    onClick={() => setTab(t.id)}
                    onKeyDown={(e) => onTabKey(e, i)}
                  >
                    {t.icon}
                    {t.label}
                    {t.count !== undefined && <span className="count">{t.count}</span>}
                  </button>
                ))}
              </div>
              <div className="tab-panel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
                {tab === "overview" && <Overview analysis={analysis} onOpenDiagnoses={() => setTab("causes")} />}
                {tab === "findings" && <Findings key={analysis.generated_at + analysis.project} loadSource={loadSource} findings={analysis.findings} />}
                {tab === "causes" && <RootCauses analysis={analysis} />}
                {tab === "graph" && (graphError ? (
                  <Notice tone="error">Knowledge graph unavailable: {graphError}</Notice>
                ) : graph && applied && sameSource(graph.key, applied) ? (
                  <GraphView graph={graph.data} />
                ) : (
                  <Loading label="Loading the knowledge graph" />
                ))}
                {tab === "export" && applied && <Exports download={exportDownload} description={exportDescription} />}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
