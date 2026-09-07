// Types mirroring the core REST API responses (core/api/rest.py).

export type MeetingStatus =
  | "recording" | "paused" | "ready" | "processing" | "transcribing"
  | "analyzing" | "done" | "failed" | "archived";

export interface Device {
  id: number;
  name: string;
  is_default: boolean;
  is_system_candidate?: boolean;
}

export interface Consent {
  text: string;
  acknowledged: boolean;
}

export interface MeetingListItem {
  id: string;
  title: string;
  project_id?: string | null;
  status: MeetingStatus;
  start_at: string | null;
  end_at: string | null;
  duration_s: number | null;
  lang: string | null;
  segments: number;
  original_path: string | null;
}

export interface Segment {
  id: string;
  start_s: number;
  end_s: number;
  text: string;
  raw_text: string;
  language: string | null;
  confidence: number | null;
  speaker_id: string | null;
  status: string;
  display_status?: string;
}

export interface Job {
  stage: string;
  status: string;
  progress: number | null;
  error: string | null;
}

export interface AnalysisSource {
  segment_id: string;
  sprecher: string;
  timestamp: string;
}

export interface AnalysisEntry {
  text: string;
  quellen: AnalysisSource[];
  verantwortlich?: string;
  deadline?: string;
}

export interface AnalysisData {
  kurzfassung: AnalysisEntry[];
  themen: AnalysisEntry[];
  entscheidungen: AnalysisEntry[];
  aufgaben: AnalysisEntry[];
  offene_fragen: AnalysisEntry[];
  naechste_schritte: AnalysisEntry[];
  risiken: AnalysisEntry[];
  wichtige_fakten: AnalysisEntry[];
  follow_ups: AnalysisEntry[];
}

export interface Analysis {
  kind: string;
  model: string;
  // Language the analysis content was written in (resolved at analysis time).
  // null/absent for legacy rows -> the UI falls back to German.
  output_lang?: string | null;
  content: string; // the validated JSON (as a string)
  markdown: string; // rendered markdown of the same content
  created_at: string | null;
}

export interface RecordingInfo {
  source: string;
  device: string | null;
  sample_rate: number | null;
  original_path: string | null;
  status: string;
  enhancement?: {
    status: "unavailable" | "pending" | "processing" | "failed" | "ready";
    current: boolean | null;
    profile_name?: string | null;
    profile?: AudioEnhancementProfile | null;
    created_at?: string | null;
    error?: string | null;
    noise_profile_mode?: "disabled" | "automatic" | "manual" | null;
    noise_profile_start_s?: number | null;
    noise_profile_end_s?: number | null;
  };
}

export interface MeetingDetail {
  id: string;
  title: string;
  title_status: string;
  project_id?: string | null;
  settings?: Record<string, unknown>;
  status: MeetingStatus;
  start_at: string | null;
  end_at: string | null;
  duration_s: number | null;
  lang: string | null;
  recording: RecordingInfo | null;
  segments: Segment[];
  jobs: Job[];
  analyses: Analysis[];
}

export interface LiveTranscriptStatus {
  enabled: boolean;
  engine?: string;
  final_count: number;
  partial_text: string;
  partial_speaker: string | null;
  last_end_s: number;
  error: string | null;
}

export interface LiveStatus {
  id: string;
  status: string;
  duration_s: number | null;
  level_db: number | null;
  peak_db: number | null;
  speaking: boolean;
  chunks: number;
  source: string | null;
  device: string | null;
  original_path: string | null;
  error: string | null;
  live?: LiveTranscriptStatus;
  live_segments?: Segment[];
}

export interface FeatureFlags {
  live_transcription: boolean;
  live_window_s?: number;
  live_period_s?: number;
  live_tail_s?: number;
  speaker_diarization: boolean;
  live_ready: boolean;
  diarization_engine: string;
  system_audio: boolean;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface DiarizeResult {
  meeting_id: string;
  status: string;
  segments: number;
  speakers: number;
  spans: number;
  engine: string;
}

export interface AsrStatus {
  name: string;
  ready: boolean;
  size: string | null;
  network_allowed: boolean;
  pipeline_max_workers?: number;
  downloads_require_confirmation: boolean;
}

export interface LlmStatus {
  provider: string;
  base_url: string;
  model: string;
  mock: boolean;
  local: boolean;
  network_used: boolean;
  busy_policy: string;
}

export interface ModelSpec {
  id: string;
  name: string;
  kind: string;
  purpose: string;
  size: string;
  runtime: string;
  languages: string;
  installed: boolean;
  selectable: boolean;
  note?: string;
}

// --- first-run setup wizard ---
export interface SetupCheck {
  setup_completed: boolean;
  setup_version: number;
  ffmpeg: boolean;
  network_allowed: boolean;
  disk_free_mb: number | null;
  asr: { model: string; size_mb: number; installed: boolean };
  ollama: {
    reachable: boolean;
    selected_model: string;
    has_model: boolean;
    install_hint: string | null;
  };
  download: SetupDownloadStatus;
}

export interface SetupDownloadStatus {
  state: "idle" | "running" | "done" | "error";
  kind: "asr" | "ollama" | null;
  model: string | null;
  file: string | null;
  downloaded_bytes: number;
  total_bytes: number | null;
  speed_bytes_per_s: number | null;
  error: string | null;
}

export interface BenchmarkResult {
  model: string;
  meeting_id: string | null;
  meeting_title: string | null;
  audio_seconds: number | null;
  processing_seconds: number;
  cpu_seconds: number;
  cpu_utilization_percent?: number;
  cpu_utilization_host_percent?: number;
  realtime_factor: number | null;
  live_possible: boolean;
  ram_max_mb_process: number;
  gpu: string;
  reference_quality: string;
  language: string | null;
  segments: Array<{ start_s: number; end_s: number; text: string; language: string | null }>;
}

export interface AudioEnhancementProfile {
  noise_reduction_enabled: boolean;
  noise_reduction_db: number;
  gate_enabled: boolean;
  gate_threshold_db: number;
  gate_ratio: number;
  gate_range: number;
  gate_attack_ms: number;
  gate_release_ms: number;
  gate_knee: number;
  hum_filter_enabled: boolean;
  hum_frequency_hz: number;
  hum_reduction_db: number;
  highpass_enabled: boolean;
  highpass_hz: number;
  presence_enabled: boolean;
  presence_db: number;
  presence_hz: number;
  presence_q: number;
  deesser_enabled: boolean;
  deesser_intensity: number;
  deesser_max_reduction: number;
  deesser_treble_keep: number;
  compressor_enabled: boolean;
  compressor_threshold_db: number;
  compressor_ratio: number;
  compressor_attack_ms: number;
  compressor_release_ms: number;
  compressor_knee: number;
  compressor_makeup_db: number;
  loudness_enabled: boolean;
  loudness_lufs: number;
  loudness_range_lu: number;
  loudness_true_peak_db: number;
  limiter_enabled: boolean;
  limiter_db: number;
  limiter_attack_ms: number;
  limiter_release_ms: number;
}

export interface AppSettings {
  asr_model: string;
  live_asr_model: string;
  live_fallback_asr_model: string;
  quality_asr_model: string;
  asr_language: string;
  analysis_language?: string;
  default_speaker_mode: "off" | "live" | "after";
  default_analysis_template: string;
  default_summary_model: string;
  quality_analysis_model: string;
  live_transcription: boolean;
  live_window_s?: number;
  live_period_s?: number;
  live_tail_s?: number;
  speaker_diarization: boolean;
  auto_analyze: boolean;
  auto_pipeline?: boolean;
  rag_enabled: boolean;
  system_audio_enabled: boolean;
  mic_enhancement_enabled?: boolean;
  audio_enhancement_profile?: string;
  audio_enhancement_profiles?: Record<string, AudioEnhancementProfile>;
  llm_base_url: string;
  ollama_base_url?: string;
  llm_model: string;
  network_allowed: boolean;
  pipeline_max_workers?: number;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  status: string;
  meetings: number;
  files: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SearchHit {
  segment_id: string;
  meeting_id: string;
  start_s: number | null;
  end_s: number | null;
  text: string | null;
  speaker_id: string | null;
  meeting_title: string | null;
  snippet: string;
  rank: number | null;
  meeting_date?: string | null;
}

export interface RagSource {
  segment_id: string;
  meeting_id: string | null;
  meeting_title: string | null;
  meeting_date: string | null;
  speaker_id: string | null;
  start_s: number | null;
  end_s: number | null;
  timestamp: string;
  jump_target: string | null;
  snippet: string;
  source_kind?: "meeting" | "document";
  file_id?: string | null;
  file_name?: string | null;
  locator?: string | null;
}

export interface RagAnswer {
  answer: string;
  sources: RagSource[];
  citations: string[];
  invalid_citations?: string[];
  grounded: boolean;
  /** "grounded" = cited real segments; "partial" = answer exists but
   *  citations could not be verified (shown with a warning); "none" =
   *  nothing found or the model explicitly declined. */
  evidence?: "grounded" | "partial" | "none";
  hits?: number;
  model?: string;
}

export interface GeneralChatAnswer {
  answer: string;
  model?: string;
  local: boolean;
  grounded: false;
  sources: RagSource[];
}

export interface IntegrityReport {
  status: "ok" | "warning" | "error";
  generated_at: string;
  summary: { errors: number; warnings: number; checks: number };
  checks: Array<{ name: string; status: string; details: string }>;
  issues: Array<{ severity: "warning" | "error"; code: string; message: string; count?: number }>;
  repair_policy: string;
}

export interface AnalyzeResult {
  meeting_id: string;
  status: string;
  kind: string;
  model: string;
  content: string;
  markdown: string;
  created_at: string | null;
}

// P5: central tasks, speakers, markers, revisions, tags.
export type TaskStatus = "offen" | "laeuft" | "erledigt";

export interface Task {
  id: string;
  meeting_id: string | null;
  meeting_title: string | null;
  project_id?: string | null;
  project_name?: string | null;
  text: string;
  owner: string | null;
  deadline: string | null;
  status: string;
  tags: string | null;
  source_segment_id: string | null;
  source?: string;
  created_at: string | null;
  display_status?: string;
  archived_at?: string | null;
  deleted_at?: string | null;
}

export interface UploadSession {
  id: string;
  filename?: string;
  total_size: number;
  received_size: number;
  progress: number;
  status: string;
  error?: string | null;
  project_id?: string | null;
}

export interface TaskOverview {
  total: number;
  by_status: Record<string, number>;
  open: number;
}

export interface Speaker {
  speaker_id: string;
  label: string | null;
  segments: number;
}

export interface Marker {
  id: string;
  at_s: number;
  text: string;
}

export interface Tag {
  tag: string;
}

export interface BackupInfo {
  id: string;
  kind: string;
  path: string;
  size: number;
  created_at: string | null;
  note: string | null;
  exists: boolean;
}

export interface BackupCreated {
  id: string;
  kind: string;
  path: string;
  size: number;
  integrity: string;
  data_zip: string | null;
  created_at: string;
  note: string;
}
