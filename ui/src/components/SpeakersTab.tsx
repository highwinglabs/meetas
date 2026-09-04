import type { Speaker } from "../types";
import { Note } from "./ui";

export default function SpeakersTab({
  speakers,
  busy,
  diarizing,
  hasSegments,
  diarNote,
  onDiarize,
  renameFor,
  renameNew,
  onRenameForChange,
  onRenameNewChange,
  onRename,
}: {
  speakers: Speaker[];
  busy: boolean;
  diarizing: boolean;
  hasSegments: boolean;
  diarNote: string | null;
  onDiarize: () => void;
  renameFor: string | null;
  renameNew: string;
  onRenameForChange: (value: string | null) => void;
  onRenameNewChange: (value: string) => void;
  onRename: (current: string) => void;
}) {
  return (
    <>
      <div className="card actions"><div className="actions-title">Sprecher</div><button className="btn" onClick={onDiarize} disabled={busy || diarizing || !hasSegments}>{diarizing ? "Sprecher werden getrennt…" : "Basis-Sprechertrennung starten"}</button>{diarNote && <Note kind="ok">{diarNote}</Note>}</div>
      <section className="block">
        <h2>Sprecher <span className="count">{speakers.length}</span></h2>
        {!speakers.length ? (
          <Note>Keine Sprecher.</Note>
        ) : (
          <div className="speaker-list">
            {speakers.map((sp) => {
              const name = sp.label ?? sp.speaker_id;
              const editing = renameFor === sp.speaker_id;
              return (
                <div key={sp.speaker_id} className="speaker-row">
                  <span className="speaker-name">{name}</span>
                  <span className="dim">{sp.segments} Segmente</span>
                  <span className="grow" />
                  {editing ? (
                    <span className="row">
                      <input value={renameNew} onChange={(e) => onRenameNewChange(e.target.value)} placeholder="Neuer Name" autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") onRename(sp.speaker_id);
                          if (e.key === "Escape") onRenameForChange(null);
                        }} />
                      <button className="btn small" onClick={() => onRename(sp.speaker_id)}>Umbenennen</button>
                      <button className="btn small" onClick={() => onRenameForChange(null)}>Abbrechen</button>
                    </span>
                  ) : (
                    <button className="btn small" onClick={() => { onRenameForChange(sp.speaker_id); onRenameNewChange(name); }}>Umbenennen</button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </>
  );
}
