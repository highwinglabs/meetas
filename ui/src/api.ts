import type {
  AnalyzeResult, AsrStatus, BackupCreated, BackupInfo, Consent, Device, DiarizeResult,
  FeatureFlags, LiveStatus, Marker, MeetingDetail, MeetingListItem,
  RagAnswer, SearchHit, Speaker, Tag, Task, TaskOverview, ChatTurn,
  AppSettings, AudioEnhancementProfile, ModelSpec, Project, UploadSession,
} from "./types";

// Same-origin by default (the core serves this UI). Override with VITE_API_BASE
// only if you host the UI on a different origin than the core.
const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

interface Health {
  status: string;
  version: string;
  ffmpeg: boolean;
  network_allowed: boolean;
  sample_rate: number;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "Keine Verbindung zum lokalen Core. Läuft `meeting-core daemon`?");
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON body (should not happen for our API) */
  }
  if (!res.ok) {
    throw new ApiError(res.status, extractMessage(body, res.status));
  }
  return body as T;
}

function extractMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    try {
      return JSON.stringify(d);
    } catch {
      return String(d);
    }
  }
  return `HTTP ${status}`;
}

const qs = (obj: Record<string, string | number | null | undefined>): string => {
  const parts = Object.entries(obj)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

export const api = {
  health: () => request<Health>("/health"),

  featureFlags: () => request<FeatureFlags>("/config/features"),
  settings: () => request<AppSettings>("/config"),
  updateSettings: (values: Partial<AppSettings>, confirmNetworkAllowed = false) =>
    request<AppSettings>("/config", {
      method: "PUT",
      body: JSON.stringify({ values, ...(confirmNetworkAllowed ? { confirm_network_allowed: true } : {}) }),
    }),
  modelCatalog: () => request<{ models: ModelSpec[] }>("/models/catalog").then((r) => r.models),

  devices: () => request<{ devices: Device[] }>("/devices").then((r) => r.devices),
  devicePreference: () => request<Record<string, unknown>>("/devices/preference"),
  updateDevicePreference: (name: string | null, index: number | null) => request<Record<string, unknown>>("/devices/preference", {
    method: "PUT", body: JSON.stringify({ name, index }),
  }),

  consent: () => request<Consent>("/consent"),
  ackConsent: (acknowledged: boolean) =>
    request<{ ok: boolean }>("/consent/ack", {
      method: "POST",
      body: JSON.stringify({ acknowledged }),
    }),

  listMeetings: (projectId: string | null = null) =>
    request<{ meetings: MeetingListItem[] }>(`/meetings${qs({ project_id: projectId })}`).then((r) => r.meetings),
  trashMeeting: (id: string) => request<Record<string, unknown>>(`/meetings/${id}`, { method: "DELETE" }),
  restoreMeeting: (id: string) => request<Record<string, unknown>>(`/meetings/${id}/restore`, { method: "POST" }),
  permanentlyDeleteMeeting: (id: string) => request<Record<string, unknown>>(`/meetings/${id}/permanent`, { method: "DELETE" }),
  trash: () => request<{ meetings: MeetingListItem[]; projects: Project[]; files: Array<Record<string, unknown>> }>("/trash"),
  getMeeting: (id: string) => request<MeetingDetail>(`/meetings/${id}`),
  updateMeeting: (id: string, values: { title?: string; project_id?: string | null }) =>
    request<{ id: string; title: string; title_status: string; project_id: string | null }>(
      `/meetings/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  audioUrl: (id: string, variant: "original" | "enhanced" = "enhanced", revision = 0) =>
    `${BASE}/meetings/${id}/audio${qs({ variant, v: revision || undefined })}`,
  enhanceAudio: (id: string, profile?: AudioEnhancementProfile, noiseProfile?: { start_s: number; end_s: number } | null, noiseProfileMode: "disabled" | "automatic" | "manual" = "disabled") => request<{ status: string; profile: string }>(`/meetings/${id}/audio/enhance`, {
    method: "POST", body: JSON.stringify({ ...(profile ? { profile } : {}), noise_profile_mode: noiseProfileMode, ...(noiseProfile ? { noise_profile_start_s: noiseProfile.start_s, noise_profile_end_s: noiseProfile.end_s } : {}) }),
  }),
  waveform: (id: string, points = 600, start_s = 0, duration_s?: number, variant: "original" | "enhanced" = "original") => request<{ duration_s: number; view_start_s: number; view_duration_s: number; peaks: number[] }>(`/meetings/${id}/audio/waveform${qs({ points, start_s, duration_s, variant: variant === "original" ? undefined : variant })}`),
  audioPreview: (id: string, start_s: number, duration_s: number, profile: AudioEnhancementProfile, noiseProfile?: { start_s: number; end_s: number } | null, noiseProfileMode: "disabled" | "automatic" | "manual" = "disabled", signal?: AbortSignal) =>
    request<{ token: string; duration_s: number }>(`/meetings/${id}/audio/preview`, {
      method: "POST", signal, body: JSON.stringify({ start_s, duration_s, profile, noise_profile_mode: noiseProfileMode, ...(noiseProfile ? { noise_profile_start_s: noiseProfile.start_s, noise_profile_end_s: noiseProfile.end_s } : {}) }),
    }),
  audioPreviewUrl: (id: string, token: string) => `${BASE}/meetings/${id}/audio/preview/${token}`,
  liveStatus: (id: string) => request<LiveStatus>(`/meetings/${id}/status`),
  startMeeting: (title: string, source: string, device_id: number | null, consent_ack: boolean,
                 project_id: string | null = null, settings: Record<string, unknown> = {}) =>
    request<{ id: string }>("/meetings", {
      method: "POST",
      body: JSON.stringify({ title, source, device_id, consent_ack, project_id, settings }),
    }),
  pause: (id: string) =>
    request<{ ok: boolean; status: string }>(`/meetings/${id}/pause`, { method: "POST" }),
  resume: (id: string) =>
    request<{ ok: boolean; status: string }>(`/meetings/${id}/resume`, { method: "POST" }),
  stop: (id: string) =>
    request<LiveStatus>(`/meetings/${id}/stop`, { method: "POST" }),

  asrStatus: (model?: string | null) => request<AsrStatus>(`/models/asr${qs({ model })}`),
  downloadAsr: (confirm: boolean, model?: string | null) =>
    request<{ name: string; ready: boolean }>("/models/asr/download", {
      method: "POST",
      body: JSON.stringify({ confirm, model }),
    }),
  installLlm: (model: string, confirm: boolean = false) =>
    request<{ name: string; installed: boolean }>("/models/llm/install", {
      method: "POST",
      body: JSON.stringify({ confirm, model }),
    }),
  benchmarkAsr: (model: string, meeting_id?: string | null) =>
    request<Record<string, unknown>>("/models/asr/benchmark", {
      method: "POST", body: JSON.stringify({ model, meeting_id: meeting_id ?? null }),
    }),
  testLlm: (model?: string | null) => request<{ ok: boolean; model: string; seconds: number; response: string }>("/models/llm/test", {
    method: "POST", body: JSON.stringify({ model: model ?? null }),
  }),

  startTranscription: (id: string, language?: string | null, model?: string | null, allow_download: boolean = false) =>
    request<{ meeting_id: string; status: string; job_id: string }>(`/meetings/${id}/transcribe/start`, {
      method: "POST", body: JSON.stringify({ language, model, allow_download }),
    }),

  search: (q: string, limit: number = 25, meetingId: string | null = null, projectId: string | null = null, meetingIds: string[] = []) =>
    request<{ query: string; count: number; results: SearchHit[] }>(`/search${qs({ q, limit, meeting_id: meetingId, project_id: projectId, meeting_ids: meetingIds.join(",") || null })}`),

  chat: (question: string, opts: { meeting_id?: string | null; project_id?: string | null;
                                   meeting_ids?: string[]; model?: string | null; limit?: number; history?: ChatTurn[] } = {}) =>
    request<RagAnswer>("/chat", { method: "POST", body: JSON.stringify({
      question, meeting_id: opts.meeting_id ?? null, project_id: opts.project_id ?? null,
      meeting_ids: opts.meeting_ids ?? [], model: opts.model ?? null, limit: opts.limit ?? 25,
      history: opts.history ?? [],
    }) }),

  exportMeeting: (id: string, format: string = "markdown") =>
    request<{ meeting_id: string; format: string; path: string; size: number; content: string }>(
      `/meetings/${id}/export${qs({ format })}`,
    ),

  analyze: (id: string, kind: string = "summary", system?: string | null, model?: string | null, template?: string | null) =>
    request<AnalyzeResult>(`/meetings/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify({ kind, system, model, template }),
    }),

  cancelAnalysis: (id: string) =>
    request<{ meeting_id: string; status: string }>(`/meetings/${id}/analysis/cancel`, { method: "POST" }),

  diarize: (id: string) =>
    request<DiarizeResult>(`/meetings/${id}/diarize`, { method: "POST" }),

  // P5: central tasks
  listTasks: (status: string | null = null, owner: string | null = null, meetingId: string | null = null, projectId: string | null = null, view: string = "active") =>
    request<{ count: number; tasks: Task[] }>(`/tasks${qs({ status, owner, meeting_id: meetingId, project_id: projectId, view })}`).then((r) => r.tasks),
  taskOverview: (view: string = "active") => request<TaskOverview>(`/tasks/overview${qs({ view })}`),
  updateTask: (task_id: string, values: { status?: string; owner?: string; text?: string; deadline?: string } = {}) =>
    request<Task>(`/tasks/${task_id}`, { method: "PATCH", body: JSON.stringify(values) }),
  createTask: (values: { text: string; meeting_id?: string | null; project_id?: string | null; responsible?: string; deadline?: string }) =>
    request<Task>("/tasks", { method: "POST", body: JSON.stringify(values) }),
  archiveTask: (id: string) => request<Record<string, unknown>>(`/tasks/${id}/archive`, { method: "POST" }),
  restoreTask: (id: string) => request<Record<string, unknown>>(`/tasks/${id}/restore`, { method: "POST" }),
  trashTask: (id: string) => request<Record<string, unknown>>(`/tasks/${id}`, { method: "DELETE" }),
  permanentlyDeleteTask: (id: string) => request<Record<string, unknown>>(`/tasks/${id}/permanent`, {
    method: "DELETE", body: JSON.stringify({ confirm: true }),
  }),
  extractTasks: (id: string) =>
    request<Record<string, unknown>>(`/meetings/${id}/extract-tasks`, { method: "POST" }),

  // P5: speakers
  listSpeakers: (id: string) =>
    request<{ meeting_id: string; speakers: Speaker[] }>(`/meetings/${id}/speakers`).then((r) => r.speakers),
  renameSpeaker: (id: string, current: string, new_name: string) =>
    request<{ meeting_id: string; from: string; to: string; updated_segments: number }>(
      `/meetings/${id}/speakers/rename`,
      { method: "POST", body: JSON.stringify({ current, new_name }) },
    ),

  // P5: markers
  listMarkers: (id: string) =>
    request<{ meeting_id: string; markers: Marker[] }>(`/meetings/${id}/markers`).then((r) => r.markers),
  addMarker: (id: string, at_s: number, text: string) =>
    request<Marker>(`/meetings/${id}/markers`, { method: "POST", body: JSON.stringify({ at_s, text }) }),
  deleteMarker: (marker_id: string) =>
    request<{ id: string; deleted: boolean }>(`/markers/${marker_id}`, { method: "DELETE" }),

  // Transcript editing
  editSegment: (id: string, segment_id: string, text: string, speaker_id: string | null = null) =>
    request<{ id: string; text: string; speaker_id: string | null }>(
      `/meetings/${id}/segments/${segment_id}/edit`,
      { method: "POST", body: JSON.stringify({ text, speaker_id }) },
    ),
  // P5: tags
  listTags: (id: string) =>
    request<{ meeting_id: string; tags: Tag[] }>(`/meetings/${id}/tags`).then((r) => r.tags),
  addTag: (id: string, value: string) =>
    request<{ meeting_id: string; tags: string[] }>(`/meetings/${id}/tags`, { method: "POST", body: JSON.stringify({ value }) }),
  removeTag: (id: string, tag: string) =>
    request<{ meeting_id: string; tags: string[] }>(`/meetings/${id}/tags/${encodeURIComponent(tag)}`, { method: "DELETE" }),
  autoTitleTags: (id: string, force: boolean = false) =>
    request<Record<string, unknown>>(`/meetings/${id}/auto-title-tags${qs({ force: force ? "true" : null })}`, { method: "POST" }),

  // P7: local backup / restore
  listBackups: () =>
    request<{ backups: BackupInfo[] }>("/backup").then((r) => r.backups),
  createBackup: (kind: string = "db", note: string = "") =>
    request<BackupCreated>("/backup", { method: "POST", body: JSON.stringify({ kind, note }) }),
  restoreBackup: (backup_id: string, confirm: boolean = false) =>
    request<Record<string, unknown>>("/backup/restore", {
      method: "POST", body: JSON.stringify({ backup_id, confirm }),
    }),
  releaseStorage: () => request<Record<string, unknown>>("/storage/release", { method: "POST" }),

  projects: (includeArchived = false) => request<{ projects: Project[] }>(`/projects${includeArchived ? "?include_archived=true" : ""}`).then((r) => r.projects),
  createProject: (name: string, description = "") => request<Project>("/projects", {
    method: "POST", body: JSON.stringify({ name, description }),
  }),
  projectDetail: (id: string) => request<Record<string, unknown>>(`/projects/${id}`),
  reindexProjectFiles: (id: string) => request<Record<string, unknown>>(`/projects/${id}/files/reindex`, { method: "POST" }),
  projectFileUrl: (id: string) => `${BASE}/project-files/${id}`,
  trashProjectFile: (id: string) => request<Record<string, unknown>>(`/project-files/${id}`, { method: "DELETE" }),
  restoreProjectFile: (id: string) => request<Record<string, unknown>>(`/project-files/${id}/restore`, { method: "POST" }),
  permanentlyDeleteProjectFile: (id: string) => request<Record<string, unknown>>(`/project-files/${id}/permanent`, { method: "DELETE" }),
  updateProject: (id: string, values: { name?: string; description?: string; status?: string }) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  trashProject: (id: string) => request<Record<string, unknown>>(`/projects/${id}`, { method: "DELETE" }),
  restoreProject: (id: string) => request<Record<string, unknown>>(`/projects/${id}/restore`, { method: "POST" }),
  permanentlyDeleteProject: (id: string) => request<Record<string, unknown>>(`/projects/${id}/permanent`, { method: "DELETE" }),
  upload: (file: File, opts: { title?: string; project_id?: string | null; start_at?: string | null; settings?: Record<string, unknown> } = {}) =>
    request<Record<string, unknown>>(`/uploads${qs({ filename: file.name, title: opts.title ?? null, project_id: opts.project_id ?? null, start_at: opts.start_at ?? null })}`, {
      method: "POST", headers: { "Content-Type": file.type || "application/octet-stream",
        "X-Meeting-Settings": JSON.stringify(opts.settings ?? {}) }, body: file,
    }),
  startUpload: (file: File, opts: { title?: string; project_id?: string | null; start_at?: string | null; settings?: Record<string, unknown> } = {}) =>
    request<UploadSession>("/uploads/start", {
      method: "POST", body: JSON.stringify({ filename: file.name, total_size: file.size,
        title: opts.title ?? null, project_id: opts.project_id ?? null,
        start_at: opts.start_at ?? null, settings: opts.settings ?? {},
        content_type: file.type || null }),
    }),
  uploadChunk: (id: string, offset: number, chunk: Blob) =>
    request<UploadSession>(
      `/uploads/${id}/chunk`, { method: "PATCH", headers: { "Content-Type": "application/octet-stream", "X-Upload-Offset": String(offset) }, body: chunk }),
  completeUpload: (id: string) => request<Record<string, unknown>>(`/uploads/${id}/complete`, { method: "POST" }),
  pauseUpload: (id: string) => request<UploadSession>(`/uploads/${id}/pause`, { method: "POST" }),
  resumeUpload: (id: string) => request<UploadSession>(`/uploads/${id}/resume`, { method: "POST" }),
  cancelUpload: (id: string) => request<Record<string, unknown>>(`/uploads/${id}`, { method: "DELETE" }),
  listUploads: () => request<{ uploads: UploadSession[] }>("/uploads").then((r) => r.uploads),
};
