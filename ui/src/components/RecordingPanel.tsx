import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { AppSettings, Device, LiveStatus, MeetingListItem, ModelSpec, Project } from "../types";
import { fmtClock, fmtHMS, levelToPct } from "../format";
import { Badge, Note } from "./ui";

const LIVE = new Set(["recording", "paused"]);
type UploadInfo = { id: string; filename?: string; received_size: number; total_size: number; progress: number; status: string; error?: string | null };
const fallback: AppSettings = { asr_model: "small", live_asr_model: "parakeet-tdt-0.6b-v3-int8", live_fallback_asr_model: "small", quality_asr_model: "small", asr_language: "auto", default_speaker_mode: "off", default_analysis_template: "standard", default_summary_model: "qwen3.5:4b", quality_analysis_model: "qwen3.8-27b-q4kxl", live_transcription: false, speaker_diarization: false, auto_pipeline: false, auto_analyze: false, rag_enabled: true, system_audio_enabled: false, mic_enhancement_enabled: false, llm_base_url: "http://127.0.0.1:8081/v1", ollama_base_url: "http://127.0.0.1:11434/v1", llm_model: "qwen3.8-27b-q4kxl", network_allowed: false };

export default function RecordingPanel({ consentOk, onNeedConsent }: { consentOk: boolean; onNeedConsent: () => void }) {
  const [active, setActive] = useState<MeetingListItem | null>(null);
  const [live, setLive] = useState<LiveStatus | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [defaults, setDefaults] = useState<AppSettings>(fallback);
  const [title, setTitle] = useState("Neues Meeting");
  const [projectId, setProjectId] = useState("");
  const [deviceId, setDeviceId] = useState<number | "">("");
  const [systemDeviceId, setSystemDeviceId] = useState<number | "">("");
  const [source, setSource] = useState<"mic" | "system" | "both">("mic");
  const [liveTranscription, setLiveTranscription] = useState(false);
  const [liveModel, setLiveModel] = useState(fallback.live_asr_model);
  const [finalModel, setFinalModel] = useState(fallback.quality_asr_model);
  const [language, setLanguage] = useState("auto");
  const [speakerMode, setSpeakerMode] = useState<"off" | "live" | "after">("off");
  const [analysisEnabled, setAnalysisEnabled] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [inputMode, setInputMode] = useState<"record" | "import">("record");
  const [analysisModel, setAnalysisModel] = useState(fallback.default_summary_model);
  const [systemAudio, setSystemAudio] = useState(false);
  const [liveSegments, setLiveSegments] = useState<NonNullable<LiveStatus["live_segments"]>>([]);
  const liveHistoryRef = useRef<HTMLDivElement | null>(null);
  const [followLive, setFollowLive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadInfo, setUploadInfo] = useState<UploadInfo | null>(null);
  const [pendingUploads, setPendingUploads] = useState<UploadInfo[]>([]);
  const [uploadPaused, setUploadPaused] = useState(false);
  const uploadControl = useRef({ paused: false, cancelled: false });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const asrModels = useMemo(() => models.filter((m) => m.kind === "asr"), [models]);
  const llmModels = useMemo(() => models.filter((m) => m.kind === "llm"), [models]);
  const installedAsrModels = useMemo(() => asrModels.filter((m) => m.installed), [asrModels]);
  const installedLlmModels = useMemo(() => llmModels.filter((m) => m.installed), [llmModels]);
  // Every selectable model must be locally usable.  The service still keeps a
  // configured fallback for an optional provider, but an unavailable model is
  // not shown as a disabled choice in the recording form.
  const liveModelChoices = installedAsrModels;
  useEffect(() => {
    const installed = llmModels.filter((m) => m.installed);
    if (installed.length && !installed.some((m) => m.id === analysisModel)) {
      setAnalysisModel(installed.find((m) => m.id === "gemma4:latest")?.id ?? installed[0].id);
    }
  }, [llmModels, analysisModel]);
  useEffect(() => {
    if (!models.length) return;
    const asrFallback = installedAsrModels[0]?.id;
    const final = defaults.quality_asr_model || defaults.asr_model;
    const analysis = defaults.default_summary_model || defaults.llm_model;
    if (installedAsrModels.length) {
      // Keep the selected value usable after the catalog arrives; only
      // installed models are exposed in the dropdown below.
      if (!installedAsrModels.some((m) => m.id === liveModel)) {
        setLiveModel(installedAsrModels.some((m) => m.id === defaults.live_asr_model)
          ? defaults.live_asr_model : (asrFallback ?? liveModel));
      }
      if (!installedAsrModels.some((m) => m.id === finalModel)) setFinalModel(installedAsrModels.some((m) => m.id === final) ? final : (asrFallback ?? finalModel));
    }
    if (installedLlmModels.length && !installedLlmModels.some((m) => m.id === analysisModel)) {
      setAnalysisModel(installedLlmModels.some((m) => m.id === analysis) ? analysis : installedLlmModels[0].id);
    }
  }, [models, defaults, asrModels, installedAsrModels, installedLlmModels, liveModel, finalModel, analysisModel]);
  const applyDefaults = (s: AppSettings) => {
    const final = s.quality_asr_model || s.asr_model;
    const analysis = s.default_summary_model || s.llm_model;
    setDefaults(s);
    setLiveTranscription(s.live_transcription);
    setLiveModel(installedAsrModels.some((m) => m.id === s.live_asr_model) ? s.live_asr_model : (installedAsrModels[0]?.id ?? s.live_asr_model));
    setFinalModel(installedAsrModels.some((m) => m.id === final) ? final : (installedAsrModels[0]?.id ?? final));
    setLanguage(s.asr_language || "auto");
    setSpeakerMode(s.default_speaker_mode);
    setAnalysisEnabled(s.auto_analyze);
    setAnalysisModel(installedLlmModels.some((m) => m.id === analysis) ? analysis : (installedLlmModels[0]?.id ?? analysis));
  };
  const findActive = useCallback(async () => { try { const ms = await api.listMeetings(); setActive(ms.find((m) => LIVE.has(m.status)) ?? null); } catch { /* offline */ } }, []);

  useEffect(() => {
    api.devices().then((d) => {
      setDevices(d);
      const def = d.find((x) => x.is_default) ?? d[0];
      if (def) setDeviceId(def.id);
      const sys = d.find((x) => x.is_system_candidate);
      if (sys) setSystemDeviceId(sys.id);
    }).catch(() => {});
    api.projects().then(setProjects).catch(() => {});
    api.modelCatalog().then((catalog) => {
      setModels(catalog);
      const installed = catalog.filter((m) => m.kind === "llm" && m.installed);
      if (installed.length) {
      setAnalysisModel((current) => installed.some((m) => m.id === current)
          ? current : (installed.find((m) => m.id === "gemma4:latest")?.id ?? installed[0].id));
      }
    }).catch(() => {});
    api.settings().then(applyDefaults).catch(() => {}); api.featureFlags().then((f) => setSystemAudio(!!f.system_audio)).catch(() => {}); api.listUploads().then((items) => setPendingUploads(items as UploadInfo[])).catch(() => {}); findActive();
  }, [findActive]);
  useEffect(() => {
    if (pendingUploads.length > 0) {
      setNotice(`${pendingUploads.length} Upload${pendingUploads.length === 1 ? " wartet" : "s warten"}. Wähle dieselbe Datei erneut, um fortzusetzen.`);
    }
  }, [pendingUploads]);
  useEffect(() => {
    if (!active) { setLive(null); setLiveSegments([]); return; }
    let stopped = false;
    const tick = async () => { try { const s = await api.liveStatus(active.id); if (stopped) return; setLive(s); setLiveSegments(s.live_segments ?? []); if (!LIVE.has(s.status)) setActive(null); } catch { if (!stopped) setActive(null); } };
    tick(); const t = setInterval(tick, 800); return () => { stopped = true; clearInterval(t); };
  }, [active]);
  useEffect(() => {
    if (followLive && liveHistoryRef.current) {
      liveHistoryRef.current.scrollTop = liveHistoryRef.current.scrollHeight;
    }
  }, [liveSegments, live?.live?.partial_text, followLive]);


  const selectedSettings = () => ({ live_transcription: liveTranscription, live_asr_model: liveModel, live_fallback_asr_model: defaults.live_fallback_asr_model, quality_asr_model: finalModel, asr_model: finalModel, language, speaker_mode: speakerMode, analysis_enabled: analysisEnabled, analysis_model: analysisModel, analysis_template: defaults.default_analysis_template, ...(deviceId !== "" ? { device_name: devices.find((device) => device.id === deviceId)?.name } : {}), ...(source === "both" && systemDeviceId !== "" ? { system_device_id: systemDeviceId } : {}) });
  const start = async () => { setBusy(true); setError(null); setNotice(null); try { if (liveTranscription && !liveModel) throw new Error("Für Live-Text muss ein installiertes Transkriptionsmodell ausgewählt sein."); if (analysisEnabled && !analysisModel) throw new Error("Für die automatische KI-Auswertung muss ein installiertes Modell ausgewählt sein."); const dev = source === "system" ? (systemDeviceId === "" ? null : systemDeviceId) : (deviceId === "" ? null : deviceId); await api.startMeeting(title.trim() || "Neues Meeting", source, dev, consentOk, projectId || null, selectedSettings()); setNotice("Aufnahme gestartet."); await findActive(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const pause = async () => { if (active) try { await api.pause(active.id); } catch (e) { setError((e as Error).message); } };
  const resume = async () => { if (active) try { await api.resume(active.id); } catch (e) { setError((e as Error).message); } };
  const stop = async () => { if (!active) return; setBusy(true); setError(null); try { await api.stop(active.id); setNotice("Aufnahme beendet und gespeichert. Weitere Schritte startest du im Meeting."); setActive(null); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const upload = async (file?: File) => {
    if (!file) return;
    setUploadBusy(true); setError(null); setNotice(null);
    const control = { paused: false, cancelled: false };
    uploadControl.current = control;
    setUploadPaused(false);
    try {
      const existing = pendingUploads.find((item) => item.filename === file.name && item.total_size === file.size && item.status !== "completed" && item.status !== "cancelled");
      let session = existing
        ? await api.resumeUpload(existing.id)
        : await api.startUpload(file, { title: title.trim() || undefined,
          project_id: projectId || null, settings: selectedSettings() });
      setPendingUploads((items) => items.filter((item) => item.id !== session.id));
      setUploadInfo(session);
      const chunkSize = 4 * 1024 * 1024;
      while (session.received_size < session.total_size) {
        if (control.cancelled) {
          await api.cancelUpload(session.id);
          setNotice("Upload abgebrochen.");
          setUploadInfo(null);
          return;
        }
        while (control.paused && !control.cancelled) {
          await new Promise((resolve) => window.setTimeout(resolve, 150));
        }
        if (control.cancelled) continue;
        session = await api.uploadChunk(session.id, session.received_size,
          file.slice(session.received_size, Math.min(session.received_size + chunkSize, session.total_size)));
        setUploadInfo(session);
      }
      const finished = await api.completeUpload(session.id);
      const result = finished.result as { meeting_id?: string } | undefined;
      setNotice(result?.meeting_id ? "Datei importiert und als Meeting gespeichert." : "Datei im Projekt gespeichert.");
      setUploadInfo(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploadBusy(false);
    }
  };
  const toggleUploadPause = () => {
    const next = !uploadControl.current.paused;
    uploadControl.current.paused = next;
    setUploadPaused(next);
    if (uploadInfo) {
      void (next ? api.pauseUpload(uploadInfo.id) : api.resumeUpload(uploadInfo.id))
        .catch((e) => setError((e as Error).message));
    }
  };
  const cancelUpload = () => { uploadControl.current.cancelled = true; };

  const cur = live?.status ?? active?.status; const pct = levelToPct(live?.level_db);
  return <div className="panel">
    <h1>{active ? "Meeting läuft" : "Start"}</h1>{error && <Note kind="error">{error}</Note>}{notice && <Note kind="ok">{notice}</Note>}
    {!active && <div className="card start-card">
      <div className="start-intro"><span className="eyebrow">NEUES MEETING</span><h2>Was möchtest du festhalten?</h2></div>
      <div className="start-mode" role="tablist" aria-label="Arbeitsweise auswählen">
        <button type="button" role="tab" aria-selected={inputMode === "record"} className={inputMode === "record" ? "start-mode-option active" : "start-mode-option"} onClick={() => setInputMode("record")}><strong>Aufnehmen</strong></button>
        <button type="button" role="tab" aria-selected={inputMode === "import"} className={inputMode === "import" ? "start-mode-option active" : "start-mode-option"} onClick={() => setInputMode("import")}><strong>Audio importieren</strong></button>
      </div>
      <label className="field"><span>Titel</span><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="z. B. Interview IT" /></label>
      <label className="field"><span>Projekt / Ordner</span><select value={projectId} onChange={(e) => setProjectId(e.target.value)}><option value="">Kein Projekt</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      {inputMode === "record" && source !== "system" && <label className="field"><span>Mikrofon</span><select value={deviceId} onChange={(e) => setDeviceId(e.target.value === "" ? "" : Number(e.target.value))}>{devices.length === 0 && <option value="">Systemstandard</option>}{devices.map((d) => <option key={d.id} value={d.id}>{d.name}{d.is_default ? " (Standard)" : ""}</option>)}</select></label>}
      <button className="btn" type="button" onClick={() => setShowAdvanced((value) => !value)}>
        {showAdvanced ? "Weitere Optionen ausblenden" : "Weitere Optionen anzeigen"}
      </button>
      {showAdvanced && <div className="advanced-options">
        {inputMode === "record" && <section className="option-group">
          <h3>Aufnahme</h3>
          <div className="settings-grid">
            <label className="field option-card"><span>Audio-Quelle</span><select value={source} onChange={(e) => setSource(e.target.value as "mic" | "system" | "both")}><option value="mic">Mikrofon</option>{systemAudio && <><option value="system">Systemaudio</option><option value="both">Mikrofon + Systemaudio</option></>}</select></label>
            {systemAudio && (source === "system" || source === "both") && <label className="field option-card"><span>Systemaudio-Gerät</span><select value={systemDeviceId} onChange={(e) => setSystemDeviceId(e.target.value === "" ? "" : Number(e.target.value))}>{devices.filter((d) => d.is_system_candidate).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>}
            <label className="field option-card"><span>Live-Transkription</span><select value={liveTranscription ? "on" : "off"} onChange={(e) => setLiveTranscription(e.target.value === "on")}><option value="off">Aus</option><option value="on">An</option></select></label>
            <label className="field option-card"><span>Live-Modell</span><select value={liveModel} onChange={(e) => setLiveModel(e.target.value)}>{liveModelChoices.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
          </div>
        </section>}
        <section className="option-group">
          <h3>Verarbeitung</h3>
          <div className="settings-grid">
            <label className="field option-card"><span>Vollständiges Transkript</span><select value={finalModel} onChange={(e) => setFinalModel(e.target.value)}>{installedAsrModels.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
            <label className="field option-card"><span>Sprache</span><select value={language} onChange={(e) => setLanguage(e.target.value)}><option value="auto">Automatisch</option><option value="de">Deutsch</option><option value="en">Englisch</option></select></label>
            <label className="field option-card"><span>Sprecher erkennen</span><select value={speakerMode} onChange={(e) => setSpeakerMode(e.target.value as "off" | "live" | "after")}><option value="off">Nicht jetzt</option><option value="after">Nach dem Meeting</option><option value="live">Während des Meetings</option></select></label>
            <label className="field option-card"><span>KI-Zusammenfassung</span><select value={analysisEnabled ? "on" : "off"} onChange={(e) => setAnalysisEnabled(e.target.value === "on")}><option value="off">Später manuell</option><option value="on">Nach dem Meeting</option></select></label>
            <label className="field option-card"><span>KI-Modell für die Auswertung</span><select value={analysisModel} onChange={(e) => setAnalysisModel(e.target.value)}>{installedLlmModels.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
          </div>
        </section>
      </div>}
      <div className="start-actions">
        {inputMode === "record" ? <div className="start-action-primary"><button className="btn primary" onClick={start} disabled={busy || !consentOk}>{busy ? "Starte…" : "● Aufnahme starten"}</button>{!consentOk && <button className="btn" onClick={onNeedConsent}>Einwilligung erteilen</button>}</div> : <label className="btn primary upload-button">{uploadBusy ? `Upload ${Math.round((uploadInfo?.progress ?? 0) * 100)} %` : "Audio-Datei auswählen"}<input type="file" hidden accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg,.opus,.aac" disabled={uploadBusy} onChange={(e) => { void upload(e.target.files?.[0]); e.currentTarget.value = ""; }} /></label>}
        {uploadInfo && <div className="row start-upload-controls"><button className="btn small" onClick={toggleUploadPause}>{uploadPaused ? "Weiter" : "Pause"}</button><button className="btn small danger" onClick={cancelUpload}>Abbrechen</button></div>}
      </div>
    </div>}
    {active && <div className="card live-card"><div className="live-head"><div><div className="live-title">{active.title}</div><div className="live-meta">{live?.device ?? "Systemstandard"}</div></div><Badge status={cur ?? active.status} /></div><div className="meter-block"><div className="meter-value">{fmtClock(live?.duration_s ?? active.duration_s)}</div><div className="meter"><div className="meter-fill" style={{ width: `${pct}%` }} /></div><div className="meter-sub"><span>{live?.speaking ? "Sprache erkannt" : "Keine Sprache erkannt"}</span></div></div>{live?.live && <div className="live-transcript"><div className="live-transcript-label"><span>Live-Text</span></div><div ref={liveHistoryRef} className="live-history" onScroll={(e) => { const el = e.currentTarget; setFollowLive(el.scrollHeight - el.scrollTop - el.clientHeight < 24); }}>{liveSegments.length === 0 && <em>Noch kein bestätigter Text.</em>}{liveSegments.map((s) => <div key={s.id} className="live-line"><span className="seg-time">{fmtHMS(s.start_s)}</span><strong>{s.speaker_id ?? "Sprecher"}</strong><span>{s.text}</span></div>)}{live.live.partial_text && <div className="live-line partial"><span className="seg-time">{fmtHMS(live.live.last_end_s)}</span><strong>{live.live.partial_speaker ?? "…"}</strong><span>{live.live.partial_text}</span></div>}</div>{!followLive && <button className="btn small" onClick={() => { setFollowLive(true); if (liveHistoryRef.current) liveHistoryRef.current.scrollTop = liveHistoryRef.current.scrollHeight; }}>Zum neuesten Eintrag</button>}{live?.live?.error && <Note kind="error">{live.live.error}</Note>}</div>}<div className="row spread">{cur === "recording" ? <button className="btn" onClick={pause}>Pause</button> : <button className="btn" onClick={resume}>Weiter</button>}<button className="btn danger" onClick={stop} disabled={busy}>{busy ? "Stoppe…" : "■ Stopp"}</button></div></div>}
  </div>;
}
