import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AppSettings, AudioEnhancementProfile, Device, ModelSpec } from "../types";
import { Note, Spinner } from "./ui";

export default function SettingsPanel() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceChoice, setDeviceChoice] = useState("system");
  const [deviceBusy, setDeviceBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savedSettings, setSavedSettings] = useState<AppSettings | null>(null);
  const [systemAudioAvailable, setSystemAudioAvailable] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, m] = await Promise.all([api.settings(), api.modelCatalog()]);
      // Preserve configured model ids exactly. A model served by llama.cpp may
      // not appear in the Ollama catalog; opening and saving this page must not
      // silently replace that configured default.
      setSettings(s);
      setSavedSettings(s);
      setModels(m);
    } catch (e) {
      setError((e as Error).message);
    }
    try {
      const [d, pref] = await Promise.all([api.devices(), api.devicePreference()]);
      setDevices(d);
      setSystemAudioAvailable(d.some((item) => item.is_system_candidate));
      const selected = typeof pref.name === "string" && pref.name.trim()
        ? d.find((item) => item.name.toLowerCase() === String(pref.name).toLowerCase())
        : undefined;
      const resolvedId = typeof pref.resolved_id === "number" ? pref.resolved_id : null;
      setDeviceChoice(selected?.id.toString() ?? (resolvedId !== null ? resolvedId.toString() : "system"));
    } catch {
      // Audio devices are optional on headless systems; settings remain usable.
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    if (!settings) return;
    const enablingNetwork = settings.network_allowed && !savedSettings?.network_allowed;
    if (enablingNetwork && !window.confirm("Externe Anbieter und Downloads erlauben? Das kann Daten an nicht-lokale Endpunkte senden.")) {
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const updated = await api.updateSettings(settings, enablingNetwork);
      setSettings(updated);
      setSavedSettings(updated);
      setMessage("Standardspeicher gespeichert.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveDevicePreference = async () => {
    setDeviceBusy(true);
    setError(null);
    setMessage(null);
    try {
      const selected = devices.find((item) => item.id.toString() === deviceChoice);
      await api.updateDevicePreference(selected?.name ?? null, selected?.id ?? null);
      setMessage(selected ? `Mikrofonpräferenz gespeichert: ${selected.name}.` : "Mikrofonpräferenz zurück auf Systemstandard gesetzt.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeviceBusy(false);
    }
  };

  if (!settings) return <div className="panel"><h1>Allgemein</h1>{error ? <Note kind="error">{error}</Note> : <Spinner label="Lade Einstellungen…" />}</div>;
  const asr = models.filter((m) => m.kind === "asr" && m.installed);
  const llm = models.filter((m) => m.kind === "llm" && m.installed);
  const set = <K extends keyof AppSettings>(key: K, value: AppSettings[K]) =>
    setSettings((current) => current ? { ...current, [key]: value } : current);
  const profileNames = Object.keys(settings.audio_enhancement_profiles ?? {});
  const profileName = settings.audio_enhancement_profile && profileNames.includes(settings.audio_enhancement_profile)
    ? settings.audio_enhancement_profile : (profileNames[0] ?? "meeting");
  const profile = settings.audio_enhancement_profiles?.[profileName];
  const setProfileValue = (key: keyof AudioEnhancementProfile, value: number) => {
    if (!profile) return;
    set("audio_enhancement_profiles", {
      ...(settings.audio_enhancement_profiles ?? {}),
      [profileName]: { ...profile, [key]: value },
    });
  };
  const setProfileToggle = (key: keyof AudioEnhancementProfile, value: boolean) => {
    if (!profile) return;
    set("audio_enhancement_profiles", {
      ...(settings.audio_enhancement_profiles ?? {}),
      [profileName]: { ...profile, [key]: value },
    });
  };
  const modelOptions = (choices: ModelSpec[]) => <>
    {choices.length === 0 && <option value="">Kein installiertes Modell verfügbar</option>}
    {choices.length > 0 && <option value="">Bitte auswählen</option>}
    {choices.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
  </>;
  const modelValue = (choices: ModelSpec[], current: string) => choices.some((model) => model.id === current) ? current : "";
  const isDirty = savedSettings !== null && JSON.stringify(settings) !== JSON.stringify(savedSettings);

  return (
    <div className="panel">
      <h1>Allgemein</h1>
      {error && <Note kind="error">{error}</Note>}
      {message && <Note kind="ok">{message}</Note>}
      {isDirty && <div className="settings-save-row settings-toolbar"><button className="btn primary" onClick={() => void save()} disabled={busy}>{busy ? "Speichere…" : "Änderungen speichern"}</button><span className="setting-help">Ungespeicherte Änderungen</span></div>}

      <div className="card">
        <h2>Automatische Verarbeitung</h2>
        <div className="settings-grid">
          <label className="check"><input type="checkbox" checked={settings.live_transcription} onChange={(e) => set("live_transcription", e.target.checked)} /> Live-Text während der Aufnahme</label>
          <label className="check"><input type="checkbox" checked={settings.auto_pipeline ?? false} onChange={(e) => set("auto_pipeline", e.target.checked)} /> Nach dem Stopp automatisch verarbeiten</label>
          <label className="check"><input type="checkbox" checked={settings.auto_analyze} disabled={!(settings.auto_pipeline ?? false)} onChange={(e) => set("auto_analyze", e.target.checked)} /> KI-Zusammenfassung automatisch erstellen</label>
          <label className="check"><input type="checkbox" checked={settings.speaker_diarization} onChange={(e) => set("speaker_diarization", e.target.checked)} /> Sprecher nach dem Meeting erkennen</label>
        </div>
        <details className="settings-details">
          <summary>Feinsteuerung für lange Aufnahmen</summary>
          <div className="settings-grid">
          <label className="field"><span>Gleichzeitige Verarbeitungen</span><input type="number" min={1} max={4} value={settings.pipeline_max_workers ?? 1} onChange={(e) => set("pipeline_max_workers", Number(e.target.value))} /></label>
          <label className="field"><span>Live-Fenster (Sekunden)</span><input type="number" min={6} max={60} step={1} value={settings.live_window_s ?? 12} onChange={(e) => set("live_window_s", Number(e.target.value))} /></label>
          <label className="field"><span>Live-Intervall (Sekunden)</span><input type="number" min={1} max={15} step={1} value={settings.live_period_s ?? 4} onChange={(e) => set("live_period_s", Number(e.target.value))} /></label>
          <label className="field"><span>Live-Nachlauf (Sekunden)</span><input type="number" min={0.5} max={10} step={0.5} value={settings.live_tail_s ?? 3} onChange={(e) => set("live_tail_s", Number(e.target.value))} /></label>
          </div>
        </details>
      </div>

      <div className="card">
        <h2>Datenschutz &amp; Verbindung</h2>
        <label className="check"><input type="checkbox" checked={settings.network_allowed} onChange={(e) => set("network_allowed", e.target.checked)} /> Internetzugriff für Downloads und externe Anbieter erlauben</label>
      </div>

      <div className="card">
        <h2>Aufnahme & Mikrofon</h2>
        <div className="row wrap">
          <label className="field"><span>Bevorzugtes Aufnahmegerät</span><select value={deviceChoice} onChange={(e) => setDeviceChoice(e.target.value)}><option value="system">Systemstandard</option>{devices.map((device) => <option key={device.id} value={device.id}>{device.name}{device.is_default ? " (Systemstandard)" : ""}</option>)}</select></label>
          <button className="btn primary" onClick={() => void saveDevicePreference()} disabled={deviceBusy}>{deviceBusy ? "Speichere…" : "Gerät speichern"}</button>
        </div>
        <label className="check"><input type="checkbox" checked={settings.mic_enhancement_enabled ?? false} onChange={(e) => set("mic_enhancement_enabled", e.target.checked)} /> Audio automatisch verbessern</label>
        {profile && <>
          <label className="field"><span>Profil für neue Aufnahmen</span><select value={profileName} onChange={(e) => set("audio_enhancement_profile", e.target.value)}>{profileNames.map((name) => <option key={name} value={name}>{name === "natural" ? "Natürlich" : name === "meeting" ? "Meeting" : name === "noisy" ? "Starkes Rauschen" : name}</option>)}</select></label>
          <details className="settings-details">
            <summary>Profil bearbeiten</summary>
            <div className="profile-effects">
              <div className={profile.noise_reduction_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.noise_reduction_enabled} onChange={(e) => setProfileToggle("noise_reduction_enabled", e.target.checked)} /><span>Rauschreduzierung</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Stärke (dB)</span><input type="number" min={0} max={20} step={1} value={profile.noise_reduction_db} disabled={!profile.noise_reduction_enabled} onChange={(e) => setProfileValue("noise_reduction_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.gate_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.gate_enabled} onChange={(e) => setProfileToggle("gate_enabled", e.target.checked)} /><span>Expander</span><span className="setting-help">Senkt leise Pausen sanft ab.</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Schwelle (dB)</span><input type="number" min={-60} max={-10} step={1} value={profile.gate_threshold_db} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_threshold_db", Number(e.target.value))} /></label>
                  <label className="field"><span>Verhältnis</span><input type="number" min={1} max={20} step={0.5} value={profile.gate_ratio} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_ratio", Number(e.target.value))} /></label>
                  <label className="field"><span>Maximale Absenkung</span><input type="number" min={0} max={1} step={0.01} value={profile.gate_range} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_range", Number(e.target.value))} /></label>
                  <label className="field"><span>Knee</span><input type="number" min={1} max={8} step={0.1} value={profile.gate_knee} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_knee", Number(e.target.value))} /></label>
                  <label className="field"><span>Attack (ms)</span><input type="number" min={1} max={200} step={1} value={profile.gate_attack_ms} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>Release (ms)</span><input type="number" min={20} max={2000} step={10} value={profile.gate_release_ms} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_release_ms", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.hum_filter_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.hum_filter_enabled} onChange={(e) => setProfileToggle("hum_filter_enabled", e.target.checked)} /><span>Brummfilter</span><span className="setting-help">Entfernt Netzbrummen bei 50 oder 60 Hz.</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Frequenz (Hz)</span><input type="number" min={50} max={60} step={10} value={profile.hum_frequency_hz} disabled={!profile.hum_filter_enabled} onChange={(e) => setProfileValue("hum_frequency_hz", Number(e.target.value))} /></label>
                  <label className="field"><span>Absenkung (dB)</span><input type="number" min={0} max={24} step={1} value={profile.hum_reduction_db} disabled={!profile.hum_filter_enabled} onChange={(e) => setProfileValue("hum_reduction_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.highpass_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.highpass_enabled} onChange={(e) => setProfileToggle("highpass_enabled", e.target.checked)} /><span>Hochpass / Trittschall</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Grenzfrequenz (Hz)</span><input type="number" min={0} max={300} step={5} value={profile.highpass_hz} disabled={!profile.highpass_enabled} onChange={(e) => setProfileValue("highpass_hz", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.presence_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.presence_enabled} onChange={(e) => setProfileToggle("presence_enabled", e.target.checked)} /><span>Sprachklarheit</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Stärke (dB)</span><input type="number" min={-6} max={6} step={0.5} value={profile.presence_db} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_db", Number(e.target.value))} /></label>
                  <label className="field"><span>Frequenz (Hz)</span><input type="number" min={1500} max={6000} step={100} value={profile.presence_hz} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_hz", Number(e.target.value))} /></label>
                  <label className="field"><span>Bandbreite (Q)</span><input type="number" min={0.3} max={4} step={0.1} value={profile.presence_q} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_q", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.deesser_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.deesser_enabled} onChange={(e) => setProfileToggle("deesser_enabled", e.target.checked)} /><span>De-Esser</span><span className="setting-help">Reduziert scharfe S-Laute.</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Intensität</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_intensity} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_intensity", Number(e.target.value))} /></label>
                  <label className="field"><span>Maximale Absenkung</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_max_reduction} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_max_reduction", Number(e.target.value))} /></label>
                  <label className="field"><span>Höhen erhalten</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_treble_keep} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_treble_keep", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.compressor_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.compressor_enabled} onChange={(e) => setProfileToggle("compressor_enabled", e.target.checked)} /><span>Kompressor</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Verhältnis</span><input type="number" min={1} max={10} step={0.5} value={profile.compressor_ratio} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_ratio", Number(e.target.value))} /></label>
                  <label className="field"><span>Schwelle (dB)</span><input type="number" min={-45} max={-3} step={1} value={profile.compressor_threshold_db} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_threshold_db", Number(e.target.value))} /></label>
                  <label className="field"><span>Attack (ms)</span><input type="number" min={1} max={200} step={1} value={profile.compressor_attack_ms} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>Release (ms)</span><input type="number" min={20} max={2000} step={10} value={profile.compressor_release_ms} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_release_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>Knee</span><input type="number" min={1} max={8} step={0.1} value={profile.compressor_knee} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_knee", Number(e.target.value))} /></label>
                  <label className="field"><span>Makeup-Gain (dB)</span><input type="number" min={0} max={12} step={0.5} value={profile.compressor_makeup_db} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_makeup_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.loudness_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.loudness_enabled} onChange={(e) => setProfileToggle("loudness_enabled", e.target.checked)} /><span>Lautheit</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Ziel-Lautheit (LUFS)</span><input type="number" min={-30} max={-12} step={1} value={profile.loudness_lufs} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_lufs", Number(e.target.value))} /></label>
                  <label className="field"><span>Dynamikbereich (LU)</span><input type="number" min={1} max={20} step={1} value={profile.loudness_range_lu} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_range_lu", Number(e.target.value))} /></label>
                  <label className="field"><span>True Peak (dB)</span><input type="number" min={-6} max={-0.1} step={0.1} value={profile.loudness_true_peak_db} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_true_peak_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.limiter_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.limiter_enabled} onChange={(e) => setProfileToggle("limiter_enabled", e.target.checked)} /><span>Limiter</span></label>
                <div className="settings-grid">
                  <label className="field"><span>Deckel (dB)</span><input type="number" min={-6} max={-0.1} step={0.5} value={profile.limiter_db} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_db", Number(e.target.value))} /></label>
                  <label className="field"><span>Attack (ms)</span><input type="number" min={1} max={100} step={1} value={profile.limiter_attack_ms} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>Release (ms)</span><input type="number" min={10} max={1000} step={10} value={profile.limiter_release_ms} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_release_ms", Number(e.target.value))} /></label>
                </div>
              </div>
            </div>
          </details>
        </>}
        {systemAudioAvailable ? <label className="check"><input type="checkbox" checked={settings.system_audio_enabled} onChange={(e) => set("system_audio_enabled", e.target.checked)} /> Systemaudio aufnehmen</label> : <div className="setting-info">Kein Systemaudio-Gerät erkannt.</div>}
      </div>

      <div className="card">
        <h2>Standardwerte für neue Meetings</h2>
        <h3 className="settings-subsection">Transkript und Analyse</h3>
        <div className="settings-grid">
          <label className="field"><span>Live-Text</span><select value={modelValue(asr, settings.live_asr_model)} onChange={(e) => set("live_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
          <label className="field"><span>Vollständiges Transkript</span><select value={modelValue(asr, settings.quality_asr_model)} onChange={(e) => set("quality_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
          <label className="field"><span>Schnelle KI-Auswertung</span><select value={modelValue(llm, settings.default_summary_model)} onChange={(e) => set("default_summary_model", e.target.value)}>{modelOptions(llm)}</select></label>
          <label className="field"><span>KI-Auswertung mit höchster Qualität</span><select value={modelValue(llm, settings.quality_analysis_model)} onChange={(e) => set("quality_analysis_model", e.target.value)}>{modelOptions(llm)}</select></label>
        </div>
        <h3 className="settings-subsection">Sprache und Sprecher</h3>
        <div className="settings-grid">
          <label className="field"><span>Sprache</span><select value={settings.asr_language} onChange={(e) => set("asr_language", e.target.value)}><option value="auto">Automatisch</option><option value="de">Deutsch</option><option value="en">Englisch</option></select></label>
          <label className="field"><span>Sprache der KI-Auswertung</span><select value={settings.analysis_language ?? "wie_transkript"} onChange={(e) => set("analysis_language", e.target.value)}><option value="wie_transkript">Wie das Transkript</option><option value="de">Deutsch</option><option value="en">Englisch</option></select></label>
          <label className="field"><span>Sprecher erkennen</span><select value={settings.default_speaker_mode} onChange={(e) => set("default_speaker_mode", e.target.value as AppSettings["default_speaker_mode"])}><option value="off">Aus</option><option value="after">Nach dem Meeting</option><option value="live">Während des Meetings</option></select></label>
          <label className="field"><span>Vorlage für KI-Auswertungen</span><select value={settings.default_analysis_template} onChange={(e) => set("default_analysis_template", e.target.value)}><option value="standard">Standard</option><option value="compact">Kurz und kompakt</option><option value="audit">Risiken und Nachweise</option><option value="action_items">Aufgaben und Fristen</option></select></label>
        </div>
      </div>

      <details className="card settings-details provider-details">
        <summary>Erweiterte technische Einstellungen</summary>
        <label className="field"><span>Ersatzmodell für Live-Text</span><select value={modelValue(asr, settings.live_fallback_asr_model)} onChange={(e) => set("live_fallback_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
        <label className="field"><span>llama.cpp / OpenAI-kompatibler Endpunkt</span><input value={settings.llm_base_url} onChange={(e) => set("llm_base_url", e.target.value)} /></label>
        <label className="field"><span>Ollama-Endpunkt</span><input value={settings.ollama_base_url ?? "http://127.0.0.1:11434/v1"} onChange={(e) => set("ollama_base_url", e.target.value)} /></label>
      </details>
    </div>
  );
}
