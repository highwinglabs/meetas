import type { ReactNode } from "react";
import type { Segment } from "../types";
import { fmtHMS } from "../format";
import { Note } from "./ui";

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
  return (
    <section className="block transcript-section">
      <button type="button" className="transcript-section-head" onClick={onToggleOpen} aria-expanded={open} aria-label={open ? "Transkript einklappen" : "Transkript aufklappen"}>
        <h2>Transkript <span className="count">{segments.length}</span></h2>
        <span className={`transcript-toggle-icon${open ? " open" : ""}`} aria-hidden="true">⌄</span>
      </button>
      {open && <>
        <div className="transcript-section-body">
          <div className="transcript-search row wrap">
          <input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="In diesem Transkript suchen…" aria-label="Lokale Transkriptsuche" />
          <span className="dim">{query.trim() ? `${matchCount} Treffer` : ""}</span>
          <button className="btn small" onClick={() => onJumpMatch(-1)} disabled={!matchCount}>Vorheriger</button>
          <button className="btn small" onClick={() => onJumpMatch(1)} disabled={!matchCount}>Nächster</button>
          </div>
          {!segments.length ? (
            <Note>Keine Segmente.</Note>
          ) : (
            <div className="transcript">
              {segments.map((s) => (
        <div key={s.id} id={`seg-${s.id}`} className={"seg" + (flashSeg === s.id ? " seg-flash" : "")}>
          <div className="seg-head">
            <button className="seg-time" onClick={() => onPlayAt(s.start_s)} title="Audio an dieser Stelle abspielen">{fmtHMS(s.start_s)}–{fmtHMS(s.end_s)}</button>
            <span className="seg-speaker">{s.speaker_id ?? "Sprecher"}</span>
            <span className="seg-actions">
              {editSegId === s.id ? (
                <>
                  <button className="btn small" onClick={onSaveEdit}>Speichern</button>
                  <button className="btn small" onClick={onCancelEdit}>Abbrechen</button>
                </>
              ) : (
                <>
                  <button className="btn small" onClick={() => onStartEdit(s)}>Bearbeiten</button>
                  <button className="seg-flag" title="Marker an dieser Stelle setzen"
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
                  <option value="">(ohne Sprecher)</option>
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
                placeholder="Marker-Notiz…" autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") onAddMarker(s.start_s);
                  if (e.key === "Escape") onCancelMarker();
                }} />
              <button className="btn small" onClick={() => onAddMarker(s.start_s)}>Markieren</button>
              <button className="btn small" onClick={onCancelMarker}>Abbrechen</button>
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
