import type { AudioEnhancementProfile } from "../types";
import { fmtHMS } from "../format";
import type { AudioSelectionRange, NoiseProfileRange } from "./AudioWaveform";
import { useI18n } from "../i18n";

export type NoiseProfileMode = "automatic" | "manual";

export default function AudioProfileSliders({
  profile,
  onPatch,
  onToggleNoiseReduction,
  noiseProfileMode,
  onNoiseProfileModeChange,
  noiseProfileRange,
  waveformDuration,
  audioRange,
  onSelectionBoundaryChange,
  onUseSelectionAsNoiseProfile,
  id,
}: {
  profile: AudioEnhancementProfile;
  onPatch: (patch: Partial<AudioEnhancementProfile>) => void;
  onToggleNoiseReduction: (enabled: boolean) => void;
  noiseProfileMode: NoiseProfileMode;
  onNoiseProfileModeChange: (mode: NoiseProfileMode) => void;
  noiseProfileRange: NoiseProfileRange | null;
  waveformDuration: number;
  audioRange: AudioSelectionRange;
  onSelectionBoundaryChange: (boundary: "start" | "end", value: number) => void;
  onUseSelectionAsNoiseProfile: () => void;
  id: string;
}) {
  const { t } = useI18n();
  const audioProfile = profile;
  return (
    <div className="audio-sliders">
      <div className={`audio-slider audio-noise-reduction${audioProfile.noise_reduction_enabled ? "" : " collapsed"}`}>
        <span><span><input type="checkbox" checked={audioProfile.noise_reduction_enabled} onChange={(e) => onToggleNoiseReduction(e.target.checked)} /> {t("audio.noise_reduction")}</span><output>{audioProfile.noise_reduction_enabled ? t("common.active") : ""}</output></span>
        {audioProfile.noise_reduction_enabled && <>
          <div className="audio-profile-mode" role="radiogroup" aria-label={t("audio.select_noise_profile")}>
            <label><input type="radio" name={`noise-profile-mode-${id}`} checked={noiseProfileMode === "automatic"} onChange={() => onNoiseProfileModeChange("automatic")} /> {t("audio.auto_detect")}</label>
            <label><input type="radio" name={`noise-profile-mode-${id}`} checked={noiseProfileMode === "manual"} onChange={() => onNoiseProfileModeChange("manual")} /> {t("audio.manual_select")}</label>
          </div>
          {noiseProfileMode === "automatic" ? (
            <p className="audio-profile-hint">{t("audio.auto_hint")}</p>
          ) : <>
            <div className="audio-selection-fields">
              <label><span>{t("audio.start")}</span><input type="number" min={0} max={Math.max(0, waveformDuration - 1)} step={0.01} value={Number(audioRange.start.toFixed(2))} onChange={(e) => onSelectionBoundaryChange("start", Number(e.target.value))} /> s</label>
              <label><span>{t("audio.end")}</span><input type="number" min={Math.min(waveformDuration, audioRange.start + 1)} max={waveformDuration} step={0.01} value={Number(Math.min(waveformDuration, audioRange.start + audioRange.duration).toFixed(2))} onChange={(e) => onSelectionBoundaryChange("end", Number(e.target.value))} /> s</label>
              <span className="dim">{t("audio.drag_hint")}</span>
            </div>
            <div className="audio-selection-actions">
              <button type="button" className="btn small" onClick={onUseSelectionAsNoiseProfile}>{t("audio.use_selection")}</button>
              <span className="dim">{noiseProfileRange ? t("audio.range_applied", { start: fmtHMS(noiseProfileRange.start_s) ?? "", end: fmtHMS(noiseProfileRange.end_s) ?? "" }) : t("audio.no_range")}</span>
            </div>
          </>}
        </>}
        {audioProfile.noise_reduction_enabled && <div className="audio-noise-strength">
          <div className="audio-noise-strength-label"><span>{t("audio.strength")}</span><output>{audioProfile.noise_reduction_db} dB</output></div>
          <input type="range" min={0} max={20} step={1} value={audioProfile.noise_reduction_db} onChange={(e) => onPatch({ noise_reduction_db: Number(e.target.value) })} />
        </div>}
      </div>
      <div className={`audio-slider${audioProfile.gate_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.gate_enabled} onChange={(e) => onPatch({ gate_enabled: e.target.checked })} /> {t("audio.expander")}</span><output>{audioProfile.gate_threshold_db} dB</output></span><input type="range" min={-60} max={-10} step={1} value={audioProfile.gate_threshold_db} onChange={(e) => onPatch({ gate_threshold_db: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.reduction")}</span><input type="number" min={0} max={1} step={0.01} value={audioProfile.gate_range} onChange={(e) => onPatch({ gate_range: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} /></label><label className="audio-inline-field"><span>{t("audio.ratio")}</span><input type="number" min={1} max={20} step={0.5} value={audioProfile.gate_ratio} onChange={(e) => onPatch({ gate_ratio: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} />:1</label><label className="audio-inline-field"><span>{t("audio.attack")}</span><input type="number" min={1} max={200} step={1} value={audioProfile.gate_attack_ms} onChange={(e) => onPatch({ gate_attack_ms: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} /> ms</label><label className="audio-inline-field"><span>{t("audio.release")}</span><input type="number" min={20} max={2000} step={10} value={audioProfile.gate_release_ms} onChange={(e) => onPatch({ gate_release_ms: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} /> ms</label><label className="audio-inline-field"><span>{t("audio.knee")}</span><input type="number" min={1} max={8} step={0.1} value={audioProfile.gate_knee} onChange={(e) => onPatch({ gate_knee: Number(e.target.value) })} disabled={!audioProfile.gate_enabled} /></label></div></div>
      <div className={`audio-slider${audioProfile.hum_filter_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.hum_filter_enabled} onChange={(e) => onPatch({ hum_filter_enabled: e.target.checked })} /> {t("audio.hum_filter")}</span><output>{audioProfile.hum_frequency_hz} Hz</output></span><input type="range" min={50} max={60} step={10} value={audioProfile.hum_frequency_hz} onChange={(e) => onPatch({ hum_frequency_hz: Number(e.target.value) })} disabled={!audioProfile.hum_filter_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.reduction")}</span><input type="number" min={0} max={24} step={1} value={audioProfile.hum_reduction_db} onChange={(e) => onPatch({ hum_reduction_db: Number(e.target.value) })} disabled={!audioProfile.hum_filter_enabled} /> dB</label></div></div>
      <label className={`audio-slider${audioProfile.highpass_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.highpass_enabled} onChange={(e) => onPatch({ highpass_enabled: e.target.checked })} /> {t("audio.highpass")}</span><output>{audioProfile.highpass_hz} Hz</output></span><input type="range" min={0} max={300} step={5} value={audioProfile.highpass_hz} onChange={(e) => onPatch({ highpass_hz: Number(e.target.value) })} disabled={!audioProfile.highpass_enabled} /></label>
      <div className={`audio-slider${audioProfile.presence_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.presence_enabled} onChange={(e) => onPatch({ presence_enabled: e.target.checked })} /> {t("audio.presence")}</span><output>{audioProfile.presence_db} dB</output></span><input type="range" min={-6} max={6} step={0.5} value={audioProfile.presence_db} onChange={(e) => onPatch({ presence_db: Number(e.target.value) })} disabled={!audioProfile.presence_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.frequency")}</span><input type="number" min={1500} max={6000} step={100} value={audioProfile.presence_hz} onChange={(e) => onPatch({ presence_hz: Number(e.target.value) })} disabled={!audioProfile.presence_enabled} /> Hz</label><label className="audio-inline-field"><span>Q</span><input type="number" min={0.3} max={4} step={0.1} value={audioProfile.presence_q} onChange={(e) => onPatch({ presence_q: Number(e.target.value) })} disabled={!audioProfile.presence_enabled} /></label></div></div>
      <div className={`audio-slider${audioProfile.deesser_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.deesser_enabled} onChange={(e) => onPatch({ deesser_enabled: e.target.checked })} /> {t("audio.deesser")}</span><output>{Math.round(audioProfile.deesser_intensity * 100)} %</output></span><input type="range" min={0} max={1} step={0.05} value={audioProfile.deesser_intensity} onChange={(e) => onPatch({ deesser_intensity: Number(e.target.value) })} disabled={!audioProfile.deesser_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.intensity")}</span><input type="number" min={0} max={1} step={0.05} value={audioProfile.deesser_intensity} onChange={(e) => onPatch({ deesser_intensity: Number(e.target.value) })} disabled={!audioProfile.deesser_enabled} /></label><label className="audio-inline-field"><span>{t("audio.max_reduction")}</span><input type="number" min={0} max={1} step={0.05} value={audioProfile.deesser_max_reduction} onChange={(e) => onPatch({ deesser_max_reduction: Number(e.target.value) })} disabled={!audioProfile.deesser_enabled} /></label><label className="audio-inline-field"><span>{t("audio.treble_keep")}</span><input type="number" min={0} max={1} step={0.05} value={audioProfile.deesser_treble_keep} onChange={(e) => onPatch({ deesser_treble_keep: Number(e.target.value) })} disabled={!audioProfile.deesser_enabled} /></label></div></div>
      <div className={`audio-slider${audioProfile.compressor_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.compressor_enabled} onChange={(e) => onPatch({ compressor_enabled: e.target.checked })} /> {t("audio.compressor")}</span><output>{audioProfile.compressor_ratio}:1</output></span><input type="range" min={1} max={10} step={0.5} value={audioProfile.compressor_ratio} onChange={(e) => onPatch({ compressor_ratio: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.threshold")}</span><input type="number" min={-45} max={-3} step={1} value={audioProfile.compressor_threshold_db} onChange={(e) => onPatch({ compressor_threshold_db: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /> dB</label><label className="audio-inline-field"><span>{t("audio.attack")}</span><input type="number" min={1} max={200} step={1} value={audioProfile.compressor_attack_ms} onChange={(e) => onPatch({ compressor_attack_ms: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /> ms</label><label className="audio-inline-field"><span>{t("audio.release")}</span><input type="number" min={20} max={2000} step={10} value={audioProfile.compressor_release_ms} onChange={(e) => onPatch({ compressor_release_ms: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /> ms</label><label className="audio-inline-field"><span>{t("audio.knee")}</span><input type="number" min={1} max={8} step={0.1} value={audioProfile.compressor_knee} onChange={(e) => onPatch({ compressor_knee: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /></label><label className="audio-inline-field"><span>{t("audio.makeup")}</span><input type="number" min={0} max={12} step={0.5} value={audioProfile.compressor_makeup_db} onChange={(e) => onPatch({ compressor_makeup_db: Number(e.target.value) })} disabled={!audioProfile.compressor_enabled} /> dB</label></div></div>
      <div className={`audio-slider${audioProfile.loudness_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.loudness_enabled} onChange={(e) => onPatch({ loudness_enabled: e.target.checked })} /> {t("audio.loudness")}</span><output>{audioProfile.loudness_lufs} LUFS</output></span><input type="range" min={-30} max={-12} step={1} value={audioProfile.loudness_lufs} onChange={(e) => onPatch({ loudness_lufs: Number(e.target.value) })} disabled={!audioProfile.loudness_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.true_peak")}</span><input type="number" min={-6} max={-0.1} step={0.1} value={audioProfile.loudness_true_peak_db} onChange={(e) => onPatch({ loudness_true_peak_db: Number(e.target.value) })} disabled={!audioProfile.loudness_enabled} /> dB</label><label className="audio-inline-field"><span>{t("audio.dyn_range")}</span><input type="number" min={1} max={20} step={1} value={audioProfile.loudness_range_lu} onChange={(e) => onPatch({ loudness_range_lu: Number(e.target.value) })} disabled={!audioProfile.loudness_enabled} /> LU</label></div></div>
      <div className={`audio-slider${audioProfile.limiter_enabled ? "" : " collapsed"}`}><span><span><input type="checkbox" checked={audioProfile.limiter_enabled} onChange={(e) => onPatch({ limiter_enabled: e.target.checked })} /> {t("audio.limiter")}</span><output>{audioProfile.limiter_db} dB</output></span><input type="range" min={-6} max={-0.1} step={0.5} value={audioProfile.limiter_db} onChange={(e) => onPatch({ limiter_db: Number(e.target.value) })} disabled={!audioProfile.limiter_enabled} /><div className="audio-control-row"><label className="audio-inline-field"><span>{t("audio.attack")}</span><input type="number" min={1} max={100} step={1} value={audioProfile.limiter_attack_ms} onChange={(e) => onPatch({ limiter_attack_ms: Number(e.target.value) })} disabled={!audioProfile.limiter_enabled} /> ms</label><label className="audio-inline-field"><span>{t("audio.release")}</span><input type="number" min={10} max={1000} step={10} value={audioProfile.limiter_release_ms} onChange={(e) => onPatch({ limiter_release_ms: Number(e.target.value) })} disabled={!audioProfile.limiter_enabled} /> ms</label></div></div>
    </div>
  );
}
