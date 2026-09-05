import { useRef } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { fmtHMS } from "../format";
import { useI18n } from "../i18n";

export type AudioSelectionRange = { start: number; duration: number };
export type NoiseProfileRange = { start_s: number; end_s: number };

export default function AudioWaveform({
  peaks,
  duration,
  viewStart,
  viewDuration,
  zoom,
  maxViewStart,
  onZoomChange,
  onViewStartChange,
  selection,
  onSelectionChange,
  noiseProfileRange,
  variant,
  previewBusy,
}: {
  peaks: number[];
  duration: number;
  viewStart: number;
  viewDuration: number;
  zoom: number;
  maxViewStart: number;
  onZoomChange: (zoom: number) => void;
  onViewStartChange: (value: number) => void;
  selection: AudioSelectionRange;
  onSelectionChange: (range: AudioSelectionRange) => void;
  noiseProfileRange: NoiseProfileRange | null;
  variant: "original" | "enhanced";
  previewBusy: boolean;
}) {
  const { t } = useI18n();
  const selectionStart = useRef<number | null>(null);

  const timeAt = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return viewStart + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * viewDuration;
  };

  const startSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    selectionStart.current = timeAt(event);
  };

  const updateSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const first = selectionStart.current;
    if (first == null) return;
    event.preventDefault();
    const second = timeAt(event);
    const start = Math.min(first, second);
    const distance = Math.abs(second - first);
    onSelectionChange({ start, duration: Math.min(30, Math.max(1, distance)) });
  };

  const finishSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const first = selectionStart.current;
    if (first == null) return;
    const second = timeAt(event);
    selectionStart.current = null;
    const distance = Math.abs(second - first);
    if (distance < 0.05) {
      const rangeDuration = Math.min(5, Math.max(1, duration - second));
      onSelectionChange({ start: second, duration: rangeDuration });
      return;
    }
    const start = Math.min(first, second);
    onSelectionChange({ start, duration: Math.min(30, Math.max(1, distance)) });
  };

  const selectionLeft = Math.max(0, Math.min(100, ((selection.start - viewStart) / viewDuration) * 100));
  const selectionRight = Math.max(0, Math.min(100, ((selection.start + selection.duration - viewStart) / viewDuration) * 100));
  const selectionWidth = Math.max(0, selectionRight - selectionLeft);

  return (
    <div className="audio-editor-waveform">
      <div className="audio-editor-waveform-head">
        <span>{t("audio.waveform")}</span>
        <span className="dim">{variant === "enhanced" ? t("audio.enhanced") : t("audio.original")}</span>
      </div>
      <div
        className="audio-waveform"
        role="slider"
        aria-label={variant === "enhanced" ? t("audio.aria_waveform_enhanced") : t("audio.aria_waveform_original")}
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        tabIndex={0}
        onPointerDown={startSelection}
        onPointerMove={updateSelection}
        onPointerUp={finishSelection}
        onPointerCancel={finishSelection}
      >
        {selectionWidth > 0 && <span className="audio-waveform-selection" style={{ left: `${selectionLeft}%`, width: `${selectionWidth}%` }} />}
        {peaks.map((peak, index) => {
          const start = viewStart + index / peaks.length * viewDuration;
          const selected = start >= selection.start && start <= selection.start + selection.duration;
          const noiseSelected = noiseProfileRange && start >= noiseProfileRange.start_s && start <= noiseProfileRange.end_s;
          const visualPeak = Math.min(100, Math.max(2, peak * 180));
          return <span key={index} className={[selected ? "selected" : "", noiseSelected ? "noise-profile" : ""].filter(Boolean).join(" ")} style={{ height: `${visualPeak}%` }} />;
        })}
      </div>
      <div className="audio-waveform-legend" aria-label={t("audio.color_legend")}>
        <span><i className="audio-legend-preview" /> {t("common.preview")}</span>
        {noiseProfileRange && <span><i className="audio-legend-noise" /> {t("audio.noise_profile")}</span>}
      </div>
      <div className="audio-editor-meta">
        <span className="dim">{t("audio.selection")} {fmtHMS(selection.start)} – {fmtHMS(Math.min(duration, selection.start + selection.duration))}</span>
        {previewBusy && <span className="dim">{t("audio.updating_preview")}</span>}
      </div>
      <div className="audio-waveform-tools">
        <label className="audio-zoom"><span>{t("audio.zoom")}</span><input type="range" min={1} max={512} step={1} value={zoom} onChange={(e) => onZoomChange(Number(e.target.value))} /><output>{Math.round(zoom)}×</output></label>
        <label className="audio-view-position"><span>{t("audio.scroll")}</span><input type="range" min={0} max={maxViewStart} step={0.01} value={viewStart} onChange={(e) => onViewStartChange(Number(e.target.value))} disabled={!maxViewStart} aria-label={t("audio.aria_scroll")} /></label>
      </div>
    </div>
  );
}
