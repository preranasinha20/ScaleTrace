import type {
  Analysis,
  AnalyzeRequest,
  GraphData,
  ModelsResponse,
  ProjectsResponse,
  SourceContext,
  UploadConfig,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const UNREACHABLE = "Cannot reach the ScaleTrace API. Check that the backend is running (python -m scaletrace.api).";

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
  }
}

function detailOf(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d: { msg?: string }) => d.msg).join("; ");
  return fallback;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError(UNREACHABLE);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      detail = detailOf(await response.json(), detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail, response.status);
  }
  return response;
}

async function json<T>(path: string, init?: RequestInit, check?: (value: unknown) => boolean): Promise<T> {
  const response = await request(path, init);
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new ApiError("The API returned a response that is not valid JSON.");
  }
  if (check && !check(value)) throw new ApiError("The API returned an unexpected response shape.");
  return value as T;
}

const post = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const isAnalysis = (v: unknown) => isObject(v) && Array.isArray(v.findings) && isObject(v.summary);
const isGraph = (v: unknown) => isObject(v) && Array.isArray(v.nodes) && Array.isArray(v.edges);
const isSource = (v: unknown) => isObject(v) && Array.isArray(v.lines);

function settings(endpoint: string, model: string | null): string {
  const params = new URLSearchParams({ endpoint });
  if (model) params.set("model", model);
  return params.toString();
}

async function saveResponse(response: Response, fallback: string): Promise<void> {
  const blob = await response.blob();
  const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "");
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = match?.[1] ?? fallback;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Upload with progress (fetch cannot report upload progress). The body is the raw ZIP. */
function uploadZip(file: File, endpoint: string, model: string | null, onProgress: (fraction: number) => void,
                   onSent: () => void): Promise<Analysis> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const params = new URLSearchParams({ filename: file.name, endpoint });
    if (model) params.set("model", model);
    xhr.open("POST", `${BASE}/api/uploads?${params}`);
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.upload.onload = () => onSent();
    xhr.onerror = () => reject(new ApiError(UNREACHABLE));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(detailOf(body, `Upload failed (${xhr.status}).`), xhr.status));
      } else if (!isAnalysis(body)) {
        reject(new ApiError("The API returned an unexpected response shape."));
      } else {
        resolve(body as Analysis);
      }
    };
    xhr.send(file);
  });
}

export const api = {
  projects: () => json<ProjectsResponse>("/api/projects", undefined, (v) => isObject(v) && Array.isArray(v.projects)),
  models: () => json<ModelsResponse>("/api/models", undefined, (v) => isObject(v) && Array.isArray(v.models)),
  analyze: (req: AnalyzeRequest) => json<Analysis>("/api/analyze", post(req), isAnalysis),
  graph: (req: AnalyzeRequest) => json<GraphData>("/api/graph", post(req), isGraph),
  source: (project: string, file: string, line: number) =>
    json<SourceContext>(`/api/source?${new URLSearchParams({ project, file, line: String(line) })}`, undefined, isSource),
  async download(format: string, req: AnalyzeRequest): Promise<void> {
    await saveResponse(await request(`/api/export/${format}`, post(req)), `scaletrace-${format}`);
  },

  uploadConfig: () => json<UploadConfig>("/api/uploads/config", undefined, (v) => isObject(v) && typeof v.max_upload_bytes === "number"),
  upload: uploadZip,
  uploadAnalyze: (id: string, endpoint: string, model: string | null) =>
    json<Analysis>(`/api/uploads/${encodeURIComponent(id)}/analyze?${settings(endpoint, model)}`, { method: "POST" }, isAnalysis),
  uploadGraph: (id: string, endpoint: string, model: string | null) =>
    json<GraphData>(`/api/uploads/${encodeURIComponent(id)}/graph?${settings(endpoint, model)}`, undefined, isGraph),
  uploadSource: (id: string, file: string, line: number) =>
    json<SourceContext>(`/api/uploads/${encodeURIComponent(id)}/source?${new URLSearchParams({ file, line: String(line) })}`, undefined, isSource),
  async uploadDownload(id: string, format: string, endpoint: string, model: string | null): Promise<void> {
    await saveResponse(await request(`/api/uploads/${encodeURIComponent(id)}/export/${format}?${settings(endpoint, model)}`), `scaletrace-${format}`);
  },
  uploadDelete: (id: string) => json<{ deleted: boolean }>(`/api/uploads/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
