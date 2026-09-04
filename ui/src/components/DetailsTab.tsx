import type { Project } from "../types";

export default function DetailsTab({
  titleDraft,
  onTitleDraftChange,
  projectDraft,
  onProjectDraftChange,
  projects,
  saving,
  onSave,
  tags,
  newTag,
  onNewTagChange,
  onAddTag,
  onRemoveTag,
  onAutoTags,
  canAutoTags,
}: {
  titleDraft: string;
  onTitleDraftChange: (value: string) => void;
  projectDraft: string;
  onProjectDraftChange: (value: string) => void;
  projects: Project[];
  saving: boolean;
  onSave: () => void;
  tags: string[];
  newTag: string;
  onNewTagChange: (value: string) => void;
  onAddTag: () => void;
  onRemoveTag: (tag: string) => void;
  onAutoTags: () => void;
  canAutoTags: boolean;
}) {
  return (
    <>
      <div className="card meeting-meta-edit">
        <h2>Meeting-Details</h2>
        <div className="row wrap">
          <label className="field grow"><span>Titel</span><input value={titleDraft} onChange={(e) => onTitleDraftChange(e.target.value)} /></label>
          <label className="field"><span>Projekt / Ordner</span><select value={projectDraft} onChange={(e) => onProjectDraftChange(e.target.value)}><option value="">Kein Projekt</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <button className="btn primary" onClick={onSave} disabled={saving || !titleDraft.trim()}>{saving ? "Speichere…" : "Änderungen speichern"}</button>
        </div>
      </div>

      <section className="block">
        <h2>Tags <span className="count">{tags.length}</span></h2>
        <div className="tag-row">
          {tags.map((t) => (
            <span key={t} className="tag-chip">
              {t}
              <button className="tag-x" onClick={() => onRemoveTag(t)} title="Tag entfernen">×</button>
            </span>
          ))}
          {tags.length === 0 && <span className="dim">Keine Tags.</span>}
        </div>
        <div className="row">
          <input className="tag-input" value={newTag} onChange={(e) => onNewTagChange(e.target.value)}
            placeholder="Neues Tag…"
            onKeyDown={(e) => { if (e.key === "Enter") onAddTag(); }} />
          <button className="btn" onClick={onAddTag} disabled={!newTag.trim()}>Tag hinzufügen</button>
          <button className="btn" onClick={onAutoTags} disabled={!canAutoTags}>Titel/Tags automatisch</button>
        </div>
      </section>
    </>
  );
}
