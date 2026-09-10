import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AppSettings, AudioEnhancementProfile, Device, ModelSpec } from "../types";
import { Note, Spinner } from "./ui";
import { useI18n, type LangPref } from "../i18n";

export default function SettingsPanel() {
  const { t, setLang, pref } = useI18n();
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
    if (enablingNetwork && !window.confirm(t("settings.network_confirm"))) {
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      // The language select is live-bound to the i18n pref (and persisted
      // there immediately); merge it in so saving the other settings can't
      // roll back a language change made in this same session.
      const updated = await api.updateSettings({ ...settings, ui_language: pref }, enablingNetwork);
      setSettings(updated);
      setSavedSettings(updated);
      setMessage(t("settings.saved"));
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
      setMessage(selected ? t("settings.device_saved", { name: selected.name }) : t("settings.device_reset"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeviceBusy(false);
    }
  };

  if (!settings) return <div className="panel"><h1>{t("settings.title")}</h1>{error ? <Note kind="error">{error}</Note> : <Spinner label={t("settings.loading")} />}</div>;
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
    {choices.length === 0 && <option value="">{t("settings.no_model")}</option>}
    {choices.length > 0 && <option value="">{t("settings.select_model")}</option>}
    {choices.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
  </>;
  const modelValue = (choices: ModelSpec[], current: string) => choices.some((model) => model.id === current) ? current : "";
  const isDirty = savedSettings !== null && JSON.stringify(settings) !== JSON.stringify(savedSettings);

  return (
    <div className="panel">
      <h1>{t("settings.title")}</h1>
      {error && <Note kind="error">{error}</Note>}
      {message && <Note kind="ok">{message}</Note>}
      {isDirty && <div className="settings-save-row settings-toolbar"><button className="btn primary" onClick={() => void save()} disabled={busy}>{busy ? t("settings.saving") : t("settings.save")}</button><span className="setting-help">{t("settings.unsaved")}</span></div>}

      <div className="card">
        <h2>{t("lang.title")}</h2>
        <p className="setting-info">{t("lang.hint")}</p>
        <div className="row">
          <label className="field"><span className="sr-only">{t("lang.title")}</span>
            <select value={pref} onChange={(e) => setLang(e.target.value as LangPref)} aria-label={t("lang.title")}>
              <option value="system">{t("lang.system")}</option>
              <option value="de">{t("lang.de")}</option>
              <option value="en">{t("lang.en")}</option>
            </select>
          </label>
        </div>
      </div>

      <div className="card">
        <h2>{t("settings.autoprocess")}</h2>
        <div className="settings-grid">
          <label className="check"><input type="checkbox" checked={settings.live_transcription} onChange={(e) => set("live_transcription", e.target.checked)} /> {t("settings.live_text")}</label>
          <label className="check"><input type="checkbox" checked={settings.auto_pipeline ?? false} onChange={(e) => set("auto_pipeline", e.target.checked)} /> {t("settings.autoprocess_after_stop")}</label>
          <label className="check"><input type="checkbox" checked={settings.auto_analyze} disabled={!(settings.auto_pipeline ?? false)} onChange={(e) => set("auto_analyze", e.target.checked)} /> {t("settings.auto_analyze")}</label>
          <label className="check"><input type="checkbox" checked={settings.speaker_diarization} onChange={(e) => set("speaker_diarization", e.target.checked)} /> {t("settings.diarize_after")}</label>
        </div>
        <details className="settings-details">
          <summary>{t("settings.advanced_long")}</summary>
          <div className="settings-grid">
          <label className="field"><span>{t("settings.max_workers")}</span><input type="number" min={1} max={4} value={settings.pipeline_max_workers ?? 1} onChange={(e) => set("pipeline_max_workers", Number(e.target.value))} /></label>
          <label className="field"><span>{t("settings.live_window")}</span><input type="number" min={6} max={60} step={1} value={settings.live_window_s ?? 12} onChange={(e) => set("live_window_s", Number(e.target.value))} /></label>
          <label className="field"><span>{t("settings.live_period")}</span><input type="number" min={1} max={15} step={1} value={settings.live_period_s ?? 4} onChange={(e) => set("live_period_s", Number(e.target.value))} /></label>
          <label className="field"><span>{t("settings.live_tail")}</span><input type="number" min={0.5} max={10} step={0.5} value={settings.live_tail_s ?? 3} onChange={(e) => set("live_tail_s", Number(e.target.value))} /></label>
          </div>
        </details>
      </div>

      <div className="card">
        <h2>{t("settings.privacy")}</h2>
        <label className="check"><input type="checkbox" checked={settings.network_allowed} onChange={(e) => set("network_allowed", e.target.checked)} /> {t("settings.network_allowed")}</label>
      </div>

      <div className="card">
        <h2>{t("settings.recording")}</h2>
        <div className="row wrap">
          <label className="field"><span>{t("settings.preferred_device")}</span><select value={deviceChoice} onChange={(e) => setDeviceChoice(e.target.value)}><option value="system">{t("settings.device_system")}</option>{devices.map((device) => <option key={device.id} value={device.id}>{device.name}{device.is_default ? t("settings.device_system_suffix") : ""}</option>)}</select></label>
          <button className="btn primary" onClick={() => void saveDevicePreference()} disabled={deviceBusy}>{deviceBusy ? t("settings.saving") : t("settings.save_device")}</button>
        </div>
        <label className="check"><input type="checkbox" checked={settings.mic_enhancement_enabled ?? false} onChange={(e) => set("mic_enhancement_enabled", e.target.checked)} /> {t("settings.mic_enhance")}</label>
        {profile && <>
          <label className="field"><span>{t("settings.profile_new")}</span><select value={profileName} onChange={(e) => set("audio_enhancement_profile", e.target.value)}>{profileNames.map((name) => <option key={name} value={name}>{name === "natural" ? t("settings.profile.natural") : name === "meeting" ? t("settings.profile.meeting") : name === "noisy" ? t("settings.profile.noisy") : name}</option>)}</select></label>
          <details className="settings-details">
            <summary>{t("settings.profile_edit")}</summary>
            <div className="profile-effects">
              <div className={profile.noise_reduction_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.noise_reduction_enabled} onChange={(e) => setProfileToggle("noise_reduction_enabled", e.target.checked)} /><span>{t("settings.fx.noise_reduction")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.strength_db")}</span><input type="number" min={0} max={20} step={1} value={profile.noise_reduction_db} disabled={!profile.noise_reduction_enabled} onChange={(e) => setProfileValue("noise_reduction_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.gate_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.gate_enabled} onChange={(e) => setProfileToggle("gate_enabled", e.target.checked)} /><span>{t("settings.fx.expander")}</span><span className="setting-help">{t("settings.fx.expander_help")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.threshold_db")}</span><input type="number" min={-60} max={-10} step={1} value={profile.gate_threshold_db} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_threshold_db", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.ratio")}</span><input type="number" min={1} max={20} step={0.5} value={profile.gate_ratio} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_ratio", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.max_reduction")}</span><input type="number" min={0} max={1} step={0.01} value={profile.gate_range} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_range", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.knee")}</span><input type="number" min={1} max={8} step={0.1} value={profile.gate_knee} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_knee", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.attack_ms")}</span><input type="number" min={1} max={200} step={1} value={profile.gate_attack_ms} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.release_ms")}</span><input type="number" min={20} max={2000} step={10} value={profile.gate_release_ms} disabled={!profile.gate_enabled} onChange={(e) => setProfileValue("gate_release_ms", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.hum_filter_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.hum_filter_enabled} onChange={(e) => setProfileToggle("hum_filter_enabled", e.target.checked)} /><span>{t("settings.fx.hum_filter")}</span><span className="setting-help">{t("settings.fx.hum_help")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.frequency_hz")}</span><input type="number" min={50} max={60} step={10} value={profile.hum_frequency_hz} disabled={!profile.hum_filter_enabled} onChange={(e) => setProfileValue("hum_frequency_hz", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.reduction_db")}</span><input type="number" min={0} max={24} step={1} value={profile.hum_reduction_db} disabled={!profile.hum_filter_enabled} onChange={(e) => setProfileValue("hum_reduction_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.highpass_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.highpass_enabled} onChange={(e) => setProfileToggle("highpass_enabled", e.target.checked)} /><span>{t("settings.fx.highpass")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.cutoff_hz")}</span><input type="number" min={0} max={300} step={5} value={profile.highpass_hz} disabled={!profile.highpass_enabled} onChange={(e) => setProfileValue("highpass_hz", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.presence_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.presence_enabled} onChange={(e) => setProfileToggle("presence_enabled", e.target.checked)} /><span>{t("settings.fx.presence")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.strength_db")}</span><input type="number" min={-6} max={6} step={0.5} value={profile.presence_db} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_db", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.frequency_hz")}</span><input type="number" min={1500} max={6000} step={100} value={profile.presence_hz} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_hz", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.bandwidth_q")}</span><input type="number" min={0.3} max={4} step={0.1} value={profile.presence_q} disabled={!profile.presence_enabled} onChange={(e) => setProfileValue("presence_q", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.deesser_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.deesser_enabled} onChange={(e) => setProfileToggle("deesser_enabled", e.target.checked)} /><span>{t("settings.fx.deesser")}</span><span className="setting-help">{t("settings.fx.deesser_help")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.intensity")}</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_intensity} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_intensity", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.max_reduction")}</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_max_reduction} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_max_reduction", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.treble_keep")}</span><input type="number" min={0} max={1} step={0.05} value={profile.deesser_treble_keep} disabled={!profile.deesser_enabled} onChange={(e) => setProfileValue("deesser_treble_keep", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.compressor_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.compressor_enabled} onChange={(e) => setProfileToggle("compressor_enabled", e.target.checked)} /><span>{t("settings.fx.compressor")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.ratio")}</span><input type="number" min={1} max={10} step={0.5} value={profile.compressor_ratio} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_ratio", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.threshold_db")}</span><input type="number" min={-45} max={-3} step={1} value={profile.compressor_threshold_db} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_threshold_db", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.attack_ms")}</span><input type="number" min={1} max={200} step={1} value={profile.compressor_attack_ms} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.release_ms")}</span><input type="number" min={20} max={2000} step={10} value={profile.compressor_release_ms} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_release_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.knee")}</span><input type="number" min={1} max={8} step={0.1} value={profile.compressor_knee} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_knee", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.makeup_db")}</span><input type="number" min={0} max={12} step={0.5} value={profile.compressor_makeup_db} disabled={!profile.compressor_enabled} onChange={(e) => setProfileValue("compressor_makeup_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.loudness_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.loudness_enabled} onChange={(e) => setProfileToggle("loudness_enabled", e.target.checked)} /><span>{t("settings.fx.loudness")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.target_lufs")}</span><input type="number" min={-30} max={-12} step={1} value={profile.loudness_lufs} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_lufs", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.range_lu")}</span><input type="number" min={1} max={20} step={1} value={profile.loudness_range_lu} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_range_lu", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.true_peak")}</span><input type="number" min={-6} max={-0.1} step={0.1} value={profile.loudness_true_peak_db} disabled={!profile.loudness_enabled} onChange={(e) => setProfileValue("loudness_true_peak_db", Number(e.target.value))} /></label>
                </div>
              </div>
              <div className={profile.limiter_enabled ? "profile-effect" : "profile-effect disabled"}>
                <label className="profile-effect-head"><input type="checkbox" checked={profile.limiter_enabled} onChange={(e) => setProfileToggle("limiter_enabled", e.target.checked)} /><span>{t("settings.fx.limiter")}</span></label>
                <div className="settings-grid">
                  <label className="field"><span>{t("settings.fx.ceiling")}</span><input type="number" min={-6} max={-0.1} step={0.5} value={profile.limiter_db} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_db", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.attack_ms")}</span><input type="number" min={1} max={100} step={1} value={profile.limiter_attack_ms} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_attack_ms", Number(e.target.value))} /></label>
                  <label className="field"><span>{t("settings.fx.release_ms")}</span><input type="number" min={10} max={1000} step={10} value={profile.limiter_release_ms} disabled={!profile.limiter_enabled} onChange={(e) => setProfileValue("limiter_release_ms", Number(e.target.value))} /></label>
                </div>
              </div>
            </div>
          </details>
        </>}
        {systemAudioAvailable ? <label className="check"><input type="checkbox" checked={settings.system_audio_enabled} onChange={(e) => set("system_audio_enabled", e.target.checked)} /> {t("settings.system_audio")}</label> : <div className="setting-info">{t("settings.no_system_audio")}</div>}
      </div>

      <div className="card">
        <h2>{t("settings.defaults")}</h2>
        <h3 className="settings-subsection">{t("settings.transcript_analysis")}</h3>
        <div className="settings-grid">
          <label className="field"><span>{t("settings.live_asr")}</span><select value={modelValue(asr, settings.live_asr_model)} onChange={(e) => set("live_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
          <label className="field"><span>{t("settings.quality_asr")}</span><select value={modelValue(asr, settings.quality_asr_model)} onChange={(e) => set("quality_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
          <label className="field"><span>{t("settings.fast_llm")}</span><select value={modelValue(llm, settings.default_summary_model)} onChange={(e) => set("default_summary_model", e.target.value)}>{modelOptions(llm)}</select></label>
          <label className="field"><span>{t("settings.quality_llm")}</span><select value={modelValue(llm, settings.quality_analysis_model)} onChange={(e) => set("quality_analysis_model", e.target.value)}>{modelOptions(llm)}</select></label>
        </div>
        <h3 className="settings-subsection">{t("settings.lang_speakers")}</h3>
        <div className="settings-grid">
          <label className="field"><span>{t("settings.asr_language")}</span><select value={settings.asr_language} onChange={(e) => set("asr_language", e.target.value)}><option value="auto">{t("settings.lang.auto")}</option><option value="de">{t("settings.lang.de")}</option><option value="en">{t("settings.lang.en")}</option></select></label>
          <label className="field"><span>{t("settings.analysis_language")}</span><select value={settings.analysis_language ?? "wie_transkript"} onChange={(e) => set("analysis_language", e.target.value)}><option value="wie_transkript">{t("settings.lang.like_transcript")}</option><option value="de">{t("settings.lang.de")}</option><option value="en">{t("settings.lang.en")}</option></select></label>
          <label className="field"><span>{t("settings.speaker_mode")}</span><select value={settings.default_speaker_mode} onChange={(e) => set("default_speaker_mode", e.target.value as AppSettings["default_speaker_mode"])}><option value="off">{t("settings.speaker.off")}</option><option value="after">{t("settings.speaker.after")}</option><option value="live">{t("settings.speaker.live")}</option></select></label>
          <label className="field"><span>{t("settings.analysis_template")}</span><select value={settings.default_analysis_template} onChange={(e) => set("default_analysis_template", e.target.value)}><option value="standard">{t("settings.template.standard")}</option><option value="compact">{t("settings.template.compact")}</option><option value="audit">{t("settings.template.audit")}</option><option value="action_items">{t("settings.template.action_items")}</option></select></label>
        </div>
      </div>

      <details className="card settings-details provider-details">
        <summary>{t("settings.advanced_technical")}</summary>
        <label className="field"><span>{t("settings.live_fallback_asr")}</span><select value={modelValue(asr, settings.live_fallback_asr_model)} onChange={(e) => set("live_fallback_asr_model", e.target.value)}>{modelOptions(asr)}</select></label>
        <label className="field"><span>{t("settings.llm_endpoint")}</span><input value={settings.llm_base_url} onChange={(e) => set("llm_base_url", e.target.value)} /></label>
        <label className="field"><span>{t("settings.ollama_endpoint")}</span><input value={settings.ollama_base_url ?? "http://127.0.0.1:11434/v1"} onChange={(e) => set("ollama_base_url", e.target.value)} /></label>
      </details>
    </div>
  );
}
