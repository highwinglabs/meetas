import type { Project } from "../types";
import { useI18n } from "../i18n";

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
  const { t } = useI18n();
  return (
    <>
      <div className="card meeting-meta-edit">
        <h2>{t("details.title")}</h2>
        <div className="row wrap">
          <label className="field grow"><span>{t("details.title_label")}</span><input value={titleDraft} onChange={(e) => onTitleDraftChange(e.target.value)} /></label>
          <label className="field"><span>{t("details.project_label")}</span><select value={projectDraft} onChange={(e) => onProjectDraftChange(e.target.value)}><option value="">{t("details.no_project")}</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <button className="btn primary" onClick={onSave} disabled={saving || !titleDraft.trim()}>{saving ? t("common.saving") : t("common.save")}</button>
        </div>
      </div>

      <section className="block">
        <h2>{t("details.tags")} <span className="count">{tags.length}</span></h2>
        <div className="tag-row">
          {tags.map((tag) => (
            <span key={tag} className="tag-chip">
              {tag}
              <button className="tag-x" onClick={() => onRemoveTag(tag)} title={t("details.remove_tag")}>×</button>
            </span>
          ))}
          {tags.length === 0 && <span className="dim">{t("details.no_tags")}</span>}
        </div>
        <div className="row">
          <input className="tag-input" value={newTag} onChange={(e) => onNewTagChange(e.target.value)}
            placeholder={t("details.new_tag_placeholder")}
            onKeyDown={(e) => { if (e.key === "Enter") onAddTag(); }} />
          <button className="btn" onClick={onAddTag} disabled={!newTag.trim()}>{t("details.add_tag")}</button>
          <button className="btn" onClick={onAutoTags} disabled={!canAutoTags}>{t("details.auto_tags")}</button>
        </div>
      </section>
    </>
  );
}
