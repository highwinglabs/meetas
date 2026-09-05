import type { ReactNode } from "react";
import type { Segment } from "../types";
import { fmtHMS } from "../format";
import { Note } from "./ui";
import { useI18n } from "../i18n";

export default function TranscriptTab({
  segments,
  open,
  onToggleOpen,
  query,
  onQueryChange,
  matchCount,
  onJumpMatch,
  onPlayAt,
  flashSeg,
  speakerOptions,
  editSegId,
  editText,
  editSpeaker,
  onEditTextChange,
  onEditSpeakerChange,
  onStartEdit,
  onSaveEdit,
  onCancelEdit,
  onFlagSegment,
  markerFor,
  markerText,
  onMarkerTextChange,
  onAddMarker,
  onCancelMarker,
  highlight,
}: {
  segments: Segment[];
  open: boolean;
  onToggleOpen: () => void;
  query: string;
  onQueryChange: (value: string) => void;
  matchCount: number;
  onJumpMatch: (offset: number) => void;
  onPlayAt: (seconds: number) => void;
  flashSeg: string | null;
  speakerOptions: string[];
  editSegId: string | null;
  editText: string;
  editSpeaker: string;
  onEditTextChange: (value: string) => void;
  onEditSpeakerChange: (value: string) => void;
  onStartEdit: (segment: Segment) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onFlagSegment: (segment: Segment) => void;
  markerFor: string | null;
  markerText: string;
  onMarkerTextChange: (value: string) => void;
  onAddMarker: (atSeconds: number) => void;
  onCancelMarker: () => void;
  highlight: (text: string) => ReactNode;
}) {
  const { t } = useI18n();
  return (
    <section className="block transcript-section">
      <button type="button" className="transcript-section-head" onClick={onToggleOpen} aria-expanded={open} aria-label={open ? t("transcript.collapse") : t("transcript.expand")}>
        <h2>{t("transcript.title")} <span className="count">{segments.length}</span></h2>
        <span className={`transcript-toggle-icon${open ? " open" : ""}`} aria-hidden="true">⌄</span>
      </button>
      {open && <>
        <div className="transcript-section-body">
          <div className="transcript-search row wrap">
          <input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder={t("transcript.search_placeholder")} aria-label={t("transcript.search_aria")} />
          <span className="dim">{query.trim() ? t("transcript.matches", { n: matchCount }) : ""}</span>
          <button className="btn small" onClick={() => onJumpMatch(-1)} disabled={!matchCount}>{t("common.prev")}</button>
          <button className="btn small" onClick={() => onJumpMatch(1)} disabled={!matchCount}>{t("common.next")}</button>
          </div>
          {!segments.length ? (
            <Note>{t("transcript.no_segments")}</Note>
          ) : (
            <div className="transcript">
              {segments.map((s) => (
        <div key={s.id} id={`seg-${s.id}`} className={"seg" + (flashSeg === s.id ? " seg-flash" : "")}>
          <div className="seg-head">
            <button className="seg-time" onClick={() => onPlayAt(s.start_s)} title={t("transcript.play_at")}>{fmtHMS(s.start_s)}–{fmtHMS(s.end_s)}</button>
            <span className="seg-speaker">{s.speaker_id ?? t("common.speaker")}</span>
            <span className="seg-actions">
              {editSegId === s.id ? (
                <>
                  <button className="btn small" onClick={onSaveEdit}>{t("common.save_short")}</button>
                  <button className="btn small" onClick={onCancelEdit}>{t("common.cancel")}</button>
                </>
              ) : (
                <>
                  <button className="btn small" onClick={() => onStartEdit(s)}>{t("common.edit")}</button>
                  <button className="seg-flag" title={t("transcript.add_marker")}
                    onClick={() => onFlagSegment(s)}>⚑</button>
                </>
              )}
            </span>
          </div>
          {editSegId === s.id ? (
            <div className="seg-edit">
              <textarea value={editText} onChange={(e) => onEditTextChange(e.target.value)} rows={3} />
              <div className="row">
                <select value={editSpeaker} onChange={(e) => onEditSpeakerChange(e.target.value)}>
                  <option value="">{t("transcript.no_speaker")}</option>
                  {speakerOptions.map((sp) => <option key={sp} value={sp}>{sp}</option>)}
                </select>
              </div>
            </div>
          ) : (
            <div className="seg-text">{highlight(s.text)}</div>
          )}
          {markerFor === s.id && (
            <div className="seg-marker-add">
              <input value={markerText} onChange={(e) => onMarkerTextChange(e.target.value)}
                placeholder={t("transcript.marker_note")} autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") onAddMarker(s.start_s);
                  if (e.key === "Escape") onCancelMarker();
                }} />
              <button className="btn small" onClick={() => onAddMarker(s.start_s)}>{t("transcript.mark")}</button>
              <button className="btn small" onClick={onCancelMarker}>{t("common.cancel")}</button>
            </div>
          )}
        </div>
              ))}
            </div>
          )}
        </div>
      </>}
    </section>
  );
}
