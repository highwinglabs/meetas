import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  AudioEnhancementProfile, ChatTurn, MeetingDetail as MDetail, Marker, ModelSpec, Project, Segment, Speaker, Task,
} from "../types";
import { fmtDate, fmtHMS } from "../format";
import { Badge, Note, Spinner } from "./ui";
import AnalysisView from "./AnalysisView";
import AudioWaveform from "./AudioWaveform";
import AudioProfileSliders, { type NoiseProfileMode } from "./AudioProfileSliders";
import ChatPanel, { type MeetingChatMessage } from "./ChatPanel";
import DetailsTab from "./DetailsTab";
import MarkersTab from "./MarkersTab";
import SpeakersTab from "./SpeakersTab";
import TasksTab from "./TasksTab";
import TranscriptTab from "./TranscriptTab";

const DEFAULT_AUDIO_PROFILE: AudioEnhancementProfile = {
  noise_reduction_enabled: false,
  noise_reduction_db: 6, highpass_hz: 80, presence_db: 2, presence_hz: 3000,
  gate_enabled: false, highpass_enabled: false, presence_enabled: false,
  compressor_enabled: false, loudness_enabled: false, limiter_enabled: false,
  gate_threshold_db: -36, gate_ratio: 2, gate_range: 0.25, gate_attack_ms: 30, gate_release_ms: 450, gate_knee: 4,
  hum_filter_enabled: false, hum_frequency_hz: 50, hum_reduction_db: 12,
  presence_q: 1, deesser_enabled: false, deesser_intensity: 0.35, deesser_max_reduction: 0.5, deesser_treble_keep: 0.5,
  compressor_threshold_db: -20, compressor_ratio: 2.5,
  compressor_attack_ms: 15, compressor_release_ms: 220, compressor_knee: 3, compressor_makeup_db: 0,
  loudness_lufs: -19, loudness_range_lu: 7, loudness_true_peak_db: -1, limiter_db: -1, limiter_attack_ms: 5, limiter_release_ms: 50,
};

// Mirrors the backend resolution (core/analysis/schema.py:resolve_output_lang +
// _output_lang_name) so the UI can show which language the analysis is written
// in. "wie_transkript" resolves to the transcript's own language.
function analysisOutputLabel(analysisLanguage: string | undefined | null, lang: string | null): string | null {
  let code = (analysisLanguage ?? "wie_transkript").trim();
  if (code === "wie_transkript" || code === "" || code === "auto") code = (lang ?? "").trim().toLowerCase();
  if (!code) return null;
  const c = code.split("-")[0].split("_")[0];
  const names: Record<string, string> = { de: "Deutsch", en: "Englisch", fr: "Französisch", es: "Spanisch", it: "Italienisch", pl: "Polnisch" };
  return names[c] ?? code;
}

export default function MeetingDetail({
  id,
  segmentId,
  onBack,
}: {
  id: string;
  segmentId?: string | null;
  onBack: () => void;
}) {
  const [detail, setDetail] = useState<MDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"transcribe" | "analyze" | "diarize" | null>(null);
  const [cancelingAnalysis, setCancelingAnalysis] = useState(false);
  const analysisStoppedRef = useRef(false);
  const [asrReady, setAsrReady] = useState<boolean | null>(null);
  const [transcriptModels, setTranscriptModels] = useState<ModelSpec[]>([]);
  const [transcriptModel, setTranscriptModel] = useState("");
  const [analysisModels, setAnalysisModels] = useState<ModelSpec[]>([]);
  const [analysisModel, setAnalysisModel] = useState("");
  const [analysisTemplate, setAnalysisTemplate] = useState("standard");
  const [analysisOpen, setAnalysisOpen] = useState(true);
  const [diarNote, setDiarNote] = useState<string | null>(null);
  const [flashSeg, setFlashSeg] = useState<string | null>(null);
  const [autoNote, setAutoNote] = useState<string | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [meetingTasks, setMeetingTasks] = useState<Task[]>([]);
  const [taskText, setTaskText] = useState("");
  const [taskOwner, setTaskOwner] = useState("");
  const [taskDeadline, setTaskDeadline] = useState("");
  const [taskCreating, setTaskCreating] = useState(false);
  const [chatQuestion, setChatQuestion] = useState("");
  const [audioVariant, setAudioVariant] = useState<"original" | "enhanced">("original");
  const [audioRevision, setAudioRevision] = useState(0);
  const [audioBusy, setAudioBusy] = useState(false);
  const [audioEditorOpen, setAudioEditorOpen] = useState(false);
  const [audioProfile, setAudioProfile] = useState<AudioEnhancementProfile>(DEFAULT_AUDIO_PROFILE);
  const [waveform, setWaveform] = useState<number[]>([]);
  const [waveformDuration, setWaveformDuration] = useState(0);
  const [audioRange, setAudioRange] = useState({ start: 0, duration: 12 });
  const [noiseProfileRange, setNoiseProfileRange] = useState<{ start_s: number; end_s: number } | null>(null);
  const [noiseProfileMode, setNoiseProfileMode] = useState<NoiseProfileMode>("automatic");
  const [waveformZoom, setWaveformZoom] = useState(1);
  const [waveformViewStart, setWaveformViewStart] = useState(0);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const audioVariantTouched = useRef(false);
  const [chatMessages, setChatMessages] = useState<MeetingChatMessage[]>([]);
  const [chatHydrated, setChatHydrated] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [titleDraft, setTitleDraft] = useState("");
  const [projectDraft, setProjectDraft] = useState("");
  const [metaBusy, setMetaBusy] = useState(false);
  const draftFor = useRef<string | null>(null);

  // Supporting meeting data is loaded on demand, not on every poll.
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [editSegId, setEditSegId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editSpeaker, setEditSpeaker] = useState("");
  const [markerFor, setMarkerFor] = useState<string | null>(null);
  const [markerText, setMarkerText] = useState("");
  const [renameFor, setRenameFor] = useState<string | null>(null);
  const [renameNew, setRenameNew] = useState("");
  const [newTag, setNewTag] = useState("");
  const [viewTab, setViewTab] = useState<"transcript" | "speakers" | "ai" | "tasks" | "markers" | "details">("transcript");
  const [transcriptOpen, setTranscriptOpen] = useState(true);
  const [transcriptQuery, setTranscriptQuery] = useState("");
  // No active result until navigation is requested; “Nächster” then starts
  // at the first match instead of silently skipping it.
  const [transcriptMatch, setTranscriptMatch] = useState(-1);
  const [exportOpen, setExportOpen] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const pendingAudioSwitch = useRef<{ time: number; playing: boolean } | null>(null);
  const audioEnhancementPending = useRef(false);
  const audioProfileHydratedFor = useRef<string | null>(null);
  const transcriptDefaultsFor = useRef<string | null>(null);
  const analysisDefaultsFor = useRef<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.getMeeting(id);
      setDetail(d);
      const appliedProfile = d.recording?.enhancement?.profile;
      if (audioProfileHydratedFor.current !== id && appliedProfile) {
        setAudioProfile(appliedProfile);
        const enhancement = d.recording?.enhancement;
        if (enhancement?.noise_profile_mode === "automatic" || enhancement?.noise_profile_mode === "manual") {
          setNoiseProfileMode(enhancement.noise_profile_mode);
        }
        if (enhancement?.noise_profile_start_s != null && enhancement?.noise_profile_end_s != null) {
          setNoiseProfileRange({ start_s: enhancement.noise_profile_start_s, end_s: enhancement.noise_profile_end_s });
        }
        audioProfileHydratedFor.current = id;
      }
      if (audioEnhancementPending.current && d.recording?.enhancement?.status === "ready") {
        setAudioRevision((revision) => revision + 1);
        audioVariantTouched.current = true;
        setAudioVariant("enhanced");
        audioEnhancementPending.current = false;
      } else if (audioEnhancementPending.current && d.recording?.enhancement?.status === "failed") {
        audioVariantTouched.current = true;
        setAudioVariant("original");
        audioEnhancementPending.current = false;
      }
      if (!audioVariantTouched.current) setAudioVariant("original");
      if (draftFor.current !== id) {
        setTitleDraft(d.title);
        setProjectDraft(d.project_id ?? "");
        draftFor.current = id;
      }
      const configuredTemplate = d.settings?.analysis_template;
      if (typeof configuredTemplate === "string" && configuredTemplate) setAnalysisTemplate(configuredTemplate);
      const configuredTranscriptModel = d.settings?.quality_asr_model || d.settings?.asr_model;
      if (typeof configuredTranscriptModel === "string" && configuredTranscriptModel) {
        setTranscriptModel((current) => current || configuredTranscriptModel);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  const loadExtras = useCallback(async () => {
    const [spk, mks, tg, tasks] = await Promise.all([
      api.listSpeakers(id).catch(() => [] as Speaker[]),
      api.listMarkers(id).catch(() => [] as Marker[]),
      api.listTags(id).catch(() => [] as { tag: string }[]),
      api.listTasks(null, null, id).catch(() => [] as Task[]),
    ]);
    setSpeakers(spk);
    setMarkers(mks);
    setTags(tg.map((t) => t.tag));
    setMeetingTasks(tasks);
  }, [id]);

  // Jump to a transcript segment (from search/RAG sources) once loaded.
  useEffect(() => {
    if (!segmentId || !detail?.segments?.some((s) => s.id === segmentId)) return;
    setFlashSeg(segmentId);
    const el = document.getElementById(`seg-${segmentId}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    const t = setTimeout(() => setFlashSeg(null), 2500);
    return () => clearTimeout(t);
  }, [segmentId, detail]);

  const loadStatuses = useCallback(async () => {
    try {
      const a = await api.asrStatus();
      setAsrReady(a.ready);
    } catch {
      setAsrReady(null);
    }
    try {
      // Model availability is independent of the LLM health endpoint. A
      // stopped LLM server must not hide installed ASR models or prevent a
      // transcript from being selected.
      const catalog = await api.modelCatalog();
      const asrChoices = catalog.filter((m) => m.kind === "asr" && m.installed);
      setTranscriptModels(asrChoices);
      const choices = catalog.filter((m) => m.kind === "llm" && m.installed);
      setAnalysisModels(choices);
      setAnalysisModel((current) => current || choices[0]?.id || "");
      const asrInstalled = asrChoices;
      setTranscriptModel((current) => current || asrInstalled[0]?.id || "");
    } catch { /* KI-Anbieter ist optional. */ }
  }, []);

  useEffect(() => {
    if (!transcriptModel) return;
    api.asrStatus(transcriptModel).then((status) => setAsrReady(status.ready)).catch(() => setAsrReady(null));
  }, [transcriptModel]);

  useEffect(() => {
    if (!transcriptModels.length) return;
    const configured = detail?.settings?.quality_asr_model || detail?.settings?.asr_model;
    const firstLoad = transcriptDefaultsFor.current !== id;
    setTranscriptModel((current) => {
      // The meeting's explicit setting is the initial choice.  Only keep a
      // manually selected value once the detail has already been hydrated;
      // otherwise the asynchronous catalog request could win the race and
      // silently replace the configured model with the first catalog entry.
      if (firstLoad && typeof configured === "string" && transcriptModels.some((model) => model.id === configured)) return configured;
      return current && transcriptModels.some((model) => model.id === current) ? current : "";
    });
    if (detail) transcriptDefaultsFor.current = id;
  }, [detail, id, transcriptModels]);

  useEffect(() => {
    if (!analysisModels.length) return;
    const configured = detail?.settings?.analysis_model;
    const firstLoad = analysisDefaultsFor.current !== id;
    setAnalysisModel((current) => {
      if (firstLoad && typeof configured === "string" && analysisModels.some((model) => model.id === configured)) return configured;
      return current && analysisModels.some((model) => model.id === current)
        ? current : analysisModels[0].id;
    });
    if (detail) analysisDefaultsFor.current = id;
  }, [detail, id, analysisModels]);

  useEffect(() => {
    draftFor.current = null;
    audioVariantTouched.current = false;
    audioEnhancementPending.current = false;
    audioProfileHydratedFor.current = null;
    setNoiseProfileRange(null);
    setNoiseProfileMode("automatic");
    setAudioRevision(0);
    transcriptDefaultsFor.current = null;
    analysisDefaultsFor.current = null;
    setAnalysisOpen(true);
    setTranscriptOpen(true);
    setChatHydrated(false);
    setChatMessages([]);
    try {
      const stored = localStorage.getItem(`meeting-chat:${id}`);
      const parsed = stored ? JSON.parse(stored) : [];
      if (Array.isArray(parsed)) {
        setChatMessages(parsed.filter((entry) => entry && typeof entry.question === "string" && entry.answer));
      }
    } catch {
      // A damaged or unavailable browser cache must never block the meeting view.
    }
    setChatHydrated(true);
    api.projects().then(setProjects).catch(() => setProjects([]));
    load();
    loadExtras();
    loadStatuses();
  }, [load, loadExtras, loadStatuses]);

  useEffect(() => {
    let cancelled = false;
    setWaveform([]);
    setPreviewUrl(null);
    setAudioEditorOpen(false);
    api.waveform(id).then((wave) => {
      if (cancelled) return;
      setWaveform(wave.peaks);
      setWaveformDuration(wave.duration_s);
      setAudioRange({ start: 0, duration: Math.min(12, Math.max(1, wave.duration_s)) });
      setWaveformZoom(1);
      setWaveformViewStart(0);
    }).catch(() => undefined);
    api.settings().then((settings) => {
      if (cancelled || audioProfileHydratedFor.current === id) return;
      const profileName = settings.audio_enhancement_profile ?? "meeting";
      const configured = settings.audio_enhancement_profiles?.[profileName];
      if (configured) setAudioProfile(configured);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [id]);

  const waveformViewDuration = Math.max(0.05, waveformDuration / waveformZoom);
  const maxWaveformViewStart = Math.max(0, waveformDuration - waveformViewDuration);
  const visibleWaveformStart = Math.min(waveformViewStart, maxWaveformViewStart);

  useEffect(() => {
    if (!waveformDuration) return;
    let cancelled = false;
    api.waveform(id, 1000, visibleWaveformStart, waveformViewDuration, audioVariant).then((wave) => {
      if (!cancelled) setWaveform(wave.peaks);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [audioVariant, id, waveformDuration, visibleWaveformStart, waveformViewDuration]);

  useEffect(() => {
    if (!chatHydrated) return;
    try {
      localStorage.setItem(`meeting-chat:${id}`, JSON.stringify(chatMessages));
    } catch {
      // Chat history is a convenience; quota/privacy settings must not break chat.
    }
  }, [id, chatMessages, chatHydrated]);

  const saveMeetingMeta = async () => {
    if (!titleDraft.trim()) return;
    setMetaBusy(true);
    setError(null);
    try {
      await api.updateMeeting(id, { title: titleDraft.trim(), project_id: projectDraft || null });
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setMetaBusy(false);
    }
  };

  const switchAudioVariant = (variant: "original" | "enhanced") => {
    if (variant === audioVariant) return;
    const player = audioRef.current;
    pendingAudioSwitch.current = player ? {
      time: player.currentTime,
      playing: !player.paused && !player.ended,
    } : null;
    audioVariantTouched.current = true;
    setAudioVariant(variant);
  };

  const restoreAudioPosition = () => {
    const player = audioRef.current;
    const pending = pendingAudioSwitch.current;
    if (!player || !pending) return;
    pendingAudioSwitch.current = null;
    player.currentTime = Math.min(pending.time, Math.max(0, (player.duration || pending.time) - 0.05));
    if (pending.playing) void player.play().catch(() => undefined);
  };

  const setAudioRangeBoundary = (boundary: "start" | "end", value: number) => {
    const next = Number.isFinite(value) ? Math.max(0, Math.min(waveformDuration, value)) : 0;
    if (boundary === "start") {
      const start = Math.min(next, Math.max(0, waveformDuration - 1));
      const end = Math.max(start + 1, audioRange.start + audioRange.duration);
      setAudioRange({ start, duration: Math.min(30, end - start) });
      return;
    }
    const end = Math.max(audioRange.start + 1, next);
    setAudioRange({ ...audioRange, duration: Math.min(30, end - audioRange.start) });
  };

  const movePreviewAwayFromNoiseProfile = (profileRange: { start_s: number; end_s: number }) => {
    if (!waveformDuration) return;
    let start = profileRange.end_s < waveformDuration ? profileRange.end_s : Math.max(0, profileRange.start_s - 12);
    let duration = Math.min(12, waveformDuration - start);
    if (duration < 1) {
      start = 0;
      duration = Math.min(12, waveformDuration);
    }
    setAudioRange({ start, duration });
    setWaveformViewStart(Math.max(0, Math.min(maxWaveformViewStart, start)));
  };

  const transcribing = detail?.status === "transcribing" && !!detail?.jobs?.some((j) => j.stage === "transcribe" && j.status !== "done" && j.status !== "failed");
  const analyzing = !!detail?.jobs?.some((j) => j.stage === "analyze" && (j.status === "running" || j.status === "pending"));
  const diarizing = !!detail?.jobs?.some((j) => j.stage === "diarize" && j.status !== "done" && j.status !== "failed");
  const processing = detail?.status === "processing" || detail?.status === "transcribing";
  const audioEnhancement = detail?.recording?.enhancement;
  const audioEnhancementProcessing = audioEnhancement?.status === "processing";
  // The main player always loads the complete recording. The short render is
  // only a preview for the editor and must never replace the A/B player.
  const audioSource = api.audioUrl(id, audioVariant, audioRevision);

  // While a long job runs, keep polling the detail (the LLM/ASR calls block the
  // originating request, so the UI otherwise only updates when that returns).
  useEffect(() => {
    if (!transcribing && !analyzing && !diarizing && !processing) return;
    const t = setInterval(() => { load(); loadStatuses(); }, 2500);
    return () => clearInterval(t);
  }, [transcribing, analyzing, diarizing, processing, load, loadStatuses]);

  useEffect(() => {
    if (!audioEnhancementProcessing) return;
    const t = setInterval(() => { load(); }, 1500);
    return () => clearInterval(t);
  }, [audioEnhancementProcessing, load]);

  useEffect(() => {
    if (!audioEditorOpen || !waveformDuration) return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setPreviewBusy(true);
      try {
        const previewMode = audioProfile.noise_reduction_enabled && (noiseProfileMode === "automatic" || noiseProfileRange) ? noiseProfileMode : "disabled";
        const result = await api.audioPreview(id, audioRange.start, audioRange.duration, audioProfile, previewMode === "manual" ? noiseProfileRange : null, previewMode, controller.signal);
        if (!cancelled) setPreviewUrl(api.audioPreviewUrl(id, result.token));
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setPreviewBusy(false);
      }
    }, 450);
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [audioEditorOpen, audioRange, audioProfile, id, noiseProfileMode, noiseProfileRange, waveformDuration]);

  const transcribe = async () => {
    setBusy("transcribe");
    setError(null);
    try {
      const selectedModel = transcriptModel;
      if (!selectedModel) {
        setError("Für die Transkription muss ein installiertes Modell ausgewählt sein.");
        return;
      }
      await api.startTranscription(id, null, selectedModel);
      load();
      loadExtras();
      loadStatuses();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const analyze = async () => {
    setBusy("analyze");
    setError(null);
    analysisStoppedRef.current = false;
    try {
      if (!analysisModel) {
        setError("Für die KI-Auswertung muss ein installiertes Modell ausgewählt sein.");
        return;
      }
      await api.analyze(id, "summary", null, analysisModel, analysisTemplate);
      if (!analysisStoppedRef.current) load();
      loadStatuses();
    } catch (e) {
      // A user stop makes the in-flight request fail/return late; that is not
      // an error to surface.
      if (!analysisStoppedRef.current) setError((e as Error).message);
    } finally {
      if (!analysisStoppedRef.current) setBusy(null);
    }
  };

  const cancelAnalysis = async () => {
    analysisStoppedRef.current = true;
    setBusy(null);
    setError(null);
    setCancelingAnalysis(true);
    try {
      await api.cancelAnalysis(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCancelingAnalysis(false);
      load();
      loadStatuses();
    }
  };

  const diarize = async () => {
    setBusy("diarize");
    setError(null);
    try {
      const r = await api.diarize(id);
      setDiarNote(`${r.speakers} Sprecher über ${r.segments} Segmente erkannt.`);
      load();
      loadExtras();
      loadStatuses();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const askMeeting = async () => {
    const question = chatQuestion.trim();
    if (!question) return;
    if (!analysisModel) {
      setError("Für Fragen muss ein installiertes KI-Modell ausgewählt sein.");
      return;
    }
    setChatBusy(true);
    setError(null);
    try {
      const history: ChatTurn[] = chatMessages.flatMap((message) => [
        { role: "user" as const, content: message.question },
        { role: "assistant" as const, content: message.answer.answer },
      ]).slice(-12);
      const answer = await api.chat(question, {
        meeting_id: id,
        model: analysisModel,
        limit: 25,
        history,
      });
      setChatMessages((current) => [...current, { question, answer }]);
      setChatQuestion("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChatBusy(false);
    }
  };

  const deleteChatMessage = (index: number) => {
    if (!window.confirm("Diesen Chat löschen?")) return;
    setChatMessages((current) => current.filter((_, messageIndex) => messageIndex !== index));
  };

  const extractTasks = async () => {
    setExtracting(true);
    setError(null);
    setAutoNote(null);
    try {
      await api.extractTasks(id);
      await loadExtras();
      setAutoNote("Aufgaben (neu) extrahiert – siehe Registerkarte „Aufgaben“.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExtracting(false);
    }
  };

  const createMeetingTask = async () => {
    const text = taskText.trim();
    if (!text) return;
    setTaskCreating(true);
    setError(null);
    try {
      await api.createTask({ text, meeting_id: id, responsible: taskOwner.trim() || undefined, deadline: taskDeadline.trim() || undefined });
      setTaskText("");
      setTaskOwner("");
      setTaskDeadline("");
      await loadExtras();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setTaskCreating(false);
    }
  };

  const updateMeetingTask = async (task: Task, values: { status?: string; owner?: string; text?: string; deadline?: string }) => {
    setError(null);
    try {
      await api.updateTask(task.id, values);
      await loadExtras();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const lifecycleMeetingTask = async (action: "archive" | "trash", task: Task) => {
    const message = action === "archive"
      ? "Aufgabe archivieren? Sie bleibt im Aufgabenbereich wiederherstellbar."
      : "Aufgabe in den Papierkorb verschieben? Sie kann wiederhergestellt werden.";
    if (!window.confirm(message)) return;
    setError(null);
    try {
      if (action === "archive") await api.archiveTask(task.id);
      else await api.trashTask(task.id);
      await loadExtras();
    } catch (e) { setError((e as Error).message); }
  };

  const exportFile = async (format: string) => {
    setError(null);
    try {
      const r = await api.exportMeeting(id, format);
      const blob = new Blob([r.content], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${id.slice(0, 8)}.${format === "markdown" ? "md" : format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const chooseExport = (format: string) => {
    setExportOpen(false);
    void exportFile(format);
  };

  // P5: segment editing (auditable)
  const startEdit = (s: Segment) => {
    setEditSegId(s.id);
    setEditText(s.text);
    setEditSpeaker(s.speaker_id ?? "");
  };
  const saveEdit = async () => {
    if (!editSegId) return;
    setError(null);
    try {
      const spk = editSpeaker.trim() ? editSpeaker : null;
      await api.editSegment(id, editSegId, editText, spk);
      setEditSegId(null);
      load();
      loadExtras();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // P5: markers
  const addMarkerAt = async (at_s: number) => {
    const txt = markerText.trim();
    if (!txt) {
      setError("Der Marker-Text darf nicht leer sein.");
      return;
    }
    setError(null);
    try {
      await api.addMarker(id, at_s, txt);
      setMarkerFor(null);
      setMarkerText("");
      loadExtras();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const deleteMarker = async (marker_id: string) => {
    setError(null);
    try {
      await api.deleteMarker(marker_id);
      loadExtras();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // P5: speaker rename
  const doRename = async (current: string) => {
    const nn = renameNew.trim();
    if (!nn) {
      setError("Der neue Sprecher-Name darf nicht leer sein.");
      return;
    }
    setError(null);
    try {
      await api.renameSpeaker(id, current, nn);
      setRenameFor(null);
      setRenameNew("");
      load();
      loadExtras();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // P5: tags
  const doAddTag = async () => {
    const v = newTag.trim();
    if (!v) return;
    setError(null);
    try {
      const r = await api.addTag(id, v);
      setTags(r.tags);
      setNewTag("");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const doRemoveTag = async (tag: string) => {
    setError(null);
    try {
      const r = await api.removeTag(id, tag);
      setTags(r.tags);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const doAutoTags = async () => {
    setError(null);
    setAutoNote(null);
    try {
      await api.autoTitleTags(id);
      load();
      loadExtras();
      setAutoNote("Titel und Tags wurden aus der Analyse abgeleitet.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const playAt = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, seconds);
    void audio.play().catch(() => { /* browser may require a user gesture */ });
  };

  const jumpToSegment = (segmentId: string) => {
    setFlashSeg(segmentId);
    document.getElementById(`seg-${segmentId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => setFlashSeg(null), 2500);
  };

  if (error && !detail) {
    return (
      <div className="panel">
        <button className="btn" onClick={onBack}>← Meetings</button>
        <Note kind="error">{error}</Note>
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="panel">
        <button className="btn" onClick={onBack}>← Meetings</button>
        <Spinner label="Wird geladen…" />
      </div>
    );
  }

  const analysis = detail.analyses[detail.analyses.length - 1];
  const failedJobs = detail.jobs.filter((j) => j.status === "failed");
  const transcribeFailed = detail.jobs.some((job) => job.stage === "transcribe" && job.status === "failed");
  const transcribeJob = detail.jobs.find((job) => job.stage === "transcribe");
  const transcribeProgress = transcribeJob?.progress != null
    ? Math.max(0, Math.min(100, Math.round(transcribeJob.progress * 100)))
    : null;
  const hasSegs = detail.segments.length > 0;
  const speakerOptions = Array.from(new Set(
    detail.segments.map((s) => s.speaker_id).filter((x): x is string => !!x),
  )).sort();
  const transcriptMatches = transcriptQuery.trim()
    ? detail.segments.filter((segment) => segment.text.toLocaleLowerCase().includes(transcriptQuery.trim().toLocaleLowerCase()))
    : [];
  const highlightTranscript = (text: string) => {
    const query = transcriptQuery.trim();
    if (!query) return text;
    const parts = text.split(new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"));
    return parts.map((part, index) => part.toLocaleLowerCase() === query.toLocaleLowerCase()
      ? <mark key={index}>{part}</mark> : part);
  };
  const jumpTranscriptMatch = (offset: number) => {
    if (!transcriptMatches.length) return;
    const current = transcriptMatch < 0 ? (offset > 0 ? -1 : 0) : transcriptMatch;
    const next = (current + offset + transcriptMatches.length) % transcriptMatches.length;
    setTranscriptMatch(next);
    document.getElementById(`seg-${transcriptMatches[next].id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <div className="panel">
      <div className="head-row">
        <button className="btn" onClick={onBack}>← Meetings</button>
        <div className="grow">
          <h1 className="detail-title">{detail.title}</h1>
          <div className="detail-meta">
            {detail.status !== "done" && <Badge status={detail.status} />}
            <span>{fmtDate(detail.start_at)}</span>
            <span>{fmtHMS(detail.duration_s)}</span>
            {detail.lang ? <span>{detail.lang}</span> : null}
          </div>
        </div>
      </div>

      {error && <Note kind="error">{error}</Note>}
      <nav className="detail-tabs" aria-label="Meeting-Bereiche">
        {([["transcript", "Transkript"], ["ai", "KI"], ["tasks", "Aufgaben"]] as const).map(([key, label]) => (
          <button key={key} className={viewTab === key ? "tab active" : "tab"} onClick={() => setViewTab(key)}>{label}</button>
        ))}
        <details className="detail-more" open={viewTab === "speakers" || viewTab === "markers" || viewTab === "details"}>
          <summary className={viewTab === "speakers" || viewTab === "markers" || viewTab === "details" ? "tab active" : "tab"}>Mehr</summary>
          <div className="detail-more-menu">
            <button className={viewTab === "speakers" ? "tab active" : "tab"} onClick={() => setViewTab("speakers")}>Sprecher</button>
            <button className={viewTab === "markers" ? "tab active" : "tab"} onClick={() => setViewTab("markers")}>Marker</button>
            <button className={viewTab === "details" ? "tab active" : "tab"} onClick={() => setViewTab("details")}>Details</button>
          </div>
        </details>
      </nav>
      {(analyzing || diarizing) && <Spinner label={analyzing ? "Erstelle KI-Auswertung…" : "Erkenne Sprecher…"} />}
      {viewTab === "transcript" && <>
      {failedJobs.map((j) => (
        <Note key={`${j.stage}-${j.error ?? "error"}`} kind="error">{j.stage}: {j.error ?? "Fehler"}</Note>
      ))}
      <div className="card actions">
        <div className="row wrap">
          <button className="btn primary" onClick={transcribe} disabled={busy !== null || transcribing || asrReady === false}>
            {transcribing ? "Transkript läuft…" : transcribeFailed ? "Transkription erneut versuchen" : "Vollständiges Transkript erstellen"}
          </button>
          {transcriptModels.length > 0 && <label className="inline-select"><span>Modell</span><select value={transcriptModel} onChange={(e) => setTranscriptModel(e.target.value)} disabled={transcribing || busy !== null}>{transcriptModels.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>}
          <span className="vsep" />
          <div className="export-menu">
            <button className="btn" type="button" onClick={() => setExportOpen((open) => !open)} disabled={!hasSegs} aria-expanded={exportOpen} aria-haspopup="menu">Exportieren ▾</button>
            {exportOpen && <div className="export-options" role="menu">
              <button type="button" role="menuitem" onClick={() => chooseExport("markdown")}>Markdown (.md)</button>
              <button type="button" role="menuitem" onClick={() => chooseExport("txt")}>Text (.txt)</button>
              <button type="button" role="menuitem" onClick={() => chooseExport("json")}>JSON (.json)</button>
            </div>}
          </div>
        </div>
        {!transcriptModels.length && <span className="dim">Kein installiertes Transkriptionsmodell verfügbar.</span>}
      </div>
      {detail.recording?.original_path && (
        <div className="card audio-player">
          <div className="audio-toolbar">
            <div className="audio-heading">
              <h2>Audio verbessern</h2>
              {audioEnhancement?.status === "processing" && <span className="audio-status">Wird optimiert …</span>}
              {audioEnhancement?.status === "failed" && <span className="audio-status error">Optimierung fehlgeschlagen: {audioEnhancement.error ?? "Unbekannter Fehler"}</span>}
              {!audioEditorOpen && audioEnhancement?.status === "ready" && audioEnhancement.current === true && <span className="audio-status current">Profil angewendet</span>}
            </div>
            <div className="audio-actions">
              <div className="segmented" role="group" aria-label="Audio-Version">
                <button type="button" disabled={audioEnhancement?.status !== "ready"} className={audioVariant === "enhanced" ? "active" : ""} onClick={() => switchAudioVariant("enhanced")}>Optimiert</button>
                <button type="button" className={audioVariant === "original" ? "active" : ""} onClick={() => switchAudioVariant("original")}>Original</button>
              </div>
            </div>
          </div>
          <audio ref={audioRef} controls preload="metadata" onLoadedMetadata={restoreAudioPosition} src={audioSource} />
          <button type="button" className="audio-editor-toggle" onClick={() => setAudioEditorOpen((open) => !open)}>
            {audioEditorOpen ? "Anpassung schließen" : "Audio anpassen"}
          </button>
          {audioEditorOpen && <div className="audio-editor">
            {previewUrl && <div className="audio-preview">
              <span>Vorschau</span>
              <audio controls preload="metadata" src={previewUrl} aria-label="Optimierte Audiovorschau" />
            </div>}
            {waveform.length > 0 && <AudioWaveform
              peaks={waveform}
              duration={waveformDuration}
              viewStart={visibleWaveformStart}
              viewDuration={waveformViewDuration}
              zoom={waveformZoom}
              maxViewStart={maxWaveformViewStart}
              onZoomChange={setWaveformZoom}
              onViewStartChange={setWaveformViewStart}
              selection={audioRange}
              onSelectionChange={setAudioRange}
              noiseProfileRange={noiseProfileRange}
              variant={audioVariant}
              previewBusy={previewBusy}
            />}
            <AudioProfileSliders
              profile={audioProfile}
              onPatch={(patch) => setAudioProfile((p) => ({ ...p, ...patch }))}
              onToggleNoiseReduction={(enabled) => {
                setAudioProfile((p) => ({ ...p, noise_reduction_enabled: enabled }));
                if (!enabled) {
                  setNoiseProfileMode("automatic");
                  setNoiseProfileRange(null);
                }
              }}
              noiseProfileMode={noiseProfileMode}
              onNoiseProfileModeChange={(mode) => {
                setNoiseProfileMode(mode);
                if (mode === "automatic") setNoiseProfileRange(null);
              }}
              noiseProfileRange={noiseProfileRange}
              waveformDuration={waveformDuration}
              audioRange={audioRange}
              onSelectionBoundaryChange={setAudioRangeBoundary}
              onUseSelectionAsNoiseProfile={() => {
                const range = { start_s: audioRange.start, end_s: Math.min(waveformDuration, audioRange.start + audioRange.duration) };
                setNoiseProfileRange(range);
                movePreviewAwayFromNoiseProfile(range);
              }}
              id={id}
            />
            <button type="button" className="btn primary audio-apply" disabled={audioBusy || previewBusy || (audioProfile.noise_reduction_enabled && noiseProfileMode === "manual" && !noiseProfileRange)} onClick={async () => {
              setAudioBusy(true); setError(null); audioEnhancementPending.current = true;
              try {
                await api.enhanceAudio(id, audioProfile, noiseProfileMode === "manual" ? noiseProfileRange : null, audioProfile.noise_reduction_enabled ? noiseProfileMode : "disabled");
                await load();
              } catch (e) { audioEnhancementPending.current = false; setError((e as Error).message); }
              finally { setAudioBusy(false); }
            }}>{audioBusy ? "Aufnahme wird optimiert …" : "Aufnahme mit diesen Werten optimieren"}</button>
          </div>}
        </div>
      )}
      {transcribing && <div className="card job-progress" aria-live="polite">
        <div className="row spread"><strong>Transkription läuft</strong><span className="dim">{transcribeProgress != null && transcribeProgress > 0 ? `${transcribeProgress} %` : "Fortschritt wird ermittelt…"}</span></div>
        <div className="progress-track" role="progressbar" aria-label="Transkriptionsfortschritt" aria-valuemin={0} aria-valuemax={100} aria-valuenow={transcribeProgress ?? 0}><div className="progress-fill" style={{ width: `${transcribeProgress ?? 3}%` }} /></div>
      </div>}
<TranscriptTab
        segments={detail.segments}
        open={transcriptOpen}
        onToggleOpen={() => setTranscriptOpen((open) => !open)}
        query={transcriptQuery}
        onQueryChange={(value) => { setTranscriptQuery(value); setTranscriptMatch(-1); }}
        matchCount={transcriptMatches.length}
        onJumpMatch={jumpTranscriptMatch}
        onPlayAt={playAt}
        flashSeg={flashSeg}
        speakerOptions={speakerOptions}
        editSegId={editSegId}
        editText={editText}
        editSpeaker={editSpeaker}
        onEditTextChange={setEditText}
        onEditSpeakerChange={setEditSpeaker}
        onStartEdit={startEdit}
        onSaveEdit={() => void saveEdit()}
        onCancelEdit={() => setEditSegId(null)}
        onFlagSegment={(s) => { setMarkerFor(s.id); setMarkerText(""); }}
        markerFor={markerFor}
        markerText={markerText}
        onMarkerTextChange={setMarkerText}
        onAddMarker={(atSeconds) => void addMarkerAt(atSeconds)}
        onCancelMarker={() => setMarkerFor(null)}
        highlight={highlightTranscript}
      />
      </>}

      {viewTab === "ai" && <>
          <div className="card ai-model-card">
            {analysisModels.length > 0 && <label className="inline-select"><span>KI-Modell</span><select value={analysisModel} onChange={(e) => setAnalysisModel(e.target.value)}>{analysisModels.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>}
            {!analysisModels.length && <span className="hint">Kein installiertes KI-Modell verfügbar.</span>}
          </div>
          <div className="card actions ai-summary-action">
            <div className="row wrap">
              <button className="btn primary" onClick={analyze} disabled={busy !== null || analyzing || !hasSegs || !analysisModel}>{analyzing ? "Analysiere…" : analysis ? "Zusammenfassung aktualisieren" : "Zusammenfassung erstellen"}</button>
              {analyzing && <button className="btn danger" onClick={cancelAnalysis} disabled={cancelingAnalysis}>{cancelingAnalysis ? "Stopp…" : "■ Stoppen"}</button>}
              <label className="inline-select"><span>Vorlage</span><select value={analysisTemplate} onChange={(e) => setAnalysisTemplate(e.target.value)}><option value="standard">Standard</option><option value="compact">Kurz und kompakt</option><option value="audit">Risiken und Nachweise</option><option value="action_items">Aufgaben und Fristen</option></select></label>
            </div>
          </div>
<ChatPanel
          question={chatQuestion}
          onQuestionChange={setChatQuestion}
          busy={chatBusy}
          canAsk={!!analysisModel}
          onAsk={() => void askMeeting()}
          messages={chatMessages}
          onClearMessages={() => setChatMessages([])}
          onDeleteMessage={deleteChatMessage}
          onJumpToSegment={jumpToSegment}
        />
      <section className="block ai-result ai-collapsible">
        <button type="button" className="ai-result-head" onClick={() => setAnalysisOpen((open) => !open)} aria-expanded={analysisOpen} aria-label={analysisOpen ? "KI-Zusammenfassung einklappen" : "KI-Zusammenfassung aufklappen"}>
          <h2>KI-Zusammenfassung</h2>
          <span className={`transcript-toggle-icon${analysisOpen ? " open" : ""}`} aria-hidden="true">⌄</span>
        </button>
        {analysisOpen && <div className="ai-result-body">
          {analysis ? (
            <AnalysisView content={analysis.content} markdown={analysis.markdown} model={analysis.model} outputLanguage={detail ? analysisOutputLabel(detail.settings?.analysis_language as string | undefined, detail.lang) : null} />
          ) : (
            <Note>{!hasSegs ? "Transkript erforderlich." : "Noch keine Zusammenfassung."}</Note>
          )}
        </div>}
      </section>
      </>}

      {viewTab === "speakers" && <>
<SpeakersTab
        speakers={speakers}
        busy={busy !== null}
        diarizing={diarizing}
        hasSegments={hasSegs}
        diarNote={diarNote}
        onDiarize={() => void diarize()}
        renameFor={renameFor}
        renameNew={renameNew}
        onRenameForChange={setRenameFor}
        onRenameNewChange={setRenameNew}
        onRename={(current) => void doRename(current)}
      />
      </>}

      {viewTab === "markers" && <>
<MarkersTab
        markers={markers}
        onDelete={(markerId) => void deleteMarker(markerId)}
      />
      </>}

      {viewTab === "details" && <>
        <DetailsTab
          titleDraft={titleDraft}
          onTitleDraftChange={setTitleDraft}
          projectDraft={projectDraft}
          onProjectDraftChange={setProjectDraft}
          projects={projects}
          saving={metaBusy}
          onSave={() => void saveMeetingMeta()}
          tags={tags}
          newTag={newTag}
          onNewTagChange={setNewTag}
          onAddTag={() => void doAddTag()}
          onRemoveTag={(tag) => void doRemoveTag(tag)}
          onAutoTags={() => void doAutoTags()}
          canAutoTags={!!analysis}
        />
      </>}

      {viewTab === "tasks" && <>
        <TasksTab
          tasks={meetingTasks}
          taskText={taskText}
          taskOwner={taskOwner}
          taskDeadline={taskDeadline}
          onTaskTextChange={setTaskText}
          onTaskOwnerChange={setTaskOwner}
          onTaskDeadlineChange={setTaskDeadline}
          creating={taskCreating}
          onCreate={() => void createMeetingTask()}
          extracting={extracting}
          canExtract={!!analysis}
          onExtract={() => void extractTasks()}
          autoNote={autoNote}
          onUpdate={(task, values) => void updateMeetingTask(task, values)}
          onLifecycle={(action, task) => void lifecycleMeetingTask(action, task)}
        />
      </>}
    </div>
  );
}
