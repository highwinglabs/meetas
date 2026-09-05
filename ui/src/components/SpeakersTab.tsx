import type { Speaker } from "../types";
import { Note } from "./ui";
import { useI18n } from "../i18n";

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
  const { t } = useI18n();
  return (
    <>
      <div className="card actions"><div className="actions-title">{t("speakers.title")}</div><button className="btn" onClick={onDiarize} disabled={busy || diarizing || !hasSegments}>{diarizing ? t("speakers.running") : t("speakers.start")}</button>{diarNote && <Note kind="ok">{diarNote}</Note>}</div>
      <section className="block">
        <h2>{t("speakers.title")} <span className="count">{speakers.length}</span></h2>
        {!speakers.length ? (
          <Note>{t("speakers.empty")}</Note>
        ) : (
          <div className="speaker-list">
            {speakers.map((sp) => {
              const name = sp.label ?? sp.speaker_id;
              const editing = renameFor === sp.speaker_id;
              return (
                <div key={sp.speaker_id} className="speaker-row">
                  <span className="speaker-name">{name}</span>
                  <span className="dim">{t("speakers.segments", { n: sp.segments })}</span>
                  <span className="grow" />
                  {editing ? (
                    <span className="row">
                      <input value={renameNew} onChange={(e) => onRenameNewChange(e.target.value)} placeholder={t("speakers.rename_placeholder")} autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") onRename(sp.speaker_id);
                          if (e.key === "Escape") onRenameForChange(null);
                        }} />
                      <button className="btn small" onClick={() => onRename(sp.speaker_id)}>{t("speakers.rename")}</button>
                      <button className="btn small" onClick={() => onRenameForChange(null)}>{t("common.cancel")}</button>
                    </span>
                  ) : (
                    <button className="btn small" onClick={() => { onRenameForChange(sp.speaker_id); onRenameNewChange(name); }}>{t("speakers.rename")}</button>
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
