import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem, Project } from "../types";
import { fmtDate } from "../format";
import { Note, Spinner } from "./ui";
import { useI18n } from "../i18n";

type TrashMeeting = MeetingListItem & { deleted_at?: string | null };
type TrashFile = { id: string; name: string; project_id: string; project_name: string; deleted_at?: string | null };

export default function TrashPanel() {
  const { t } = useI18n();
  const [meetings, setMeetings] = useState<TrashMeeting[]>([]);
  const [projects, setProjects] = useState<Array<Project & { deleted_at?: string | null }>>([]);
  const [files, setFiles] = useState<TrashFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const result = await api.trash();
      setMeetings(result.meetings as TrashMeeting[]);
      setProjects(result.projects as Array<Project & { deleted_at?: string | null }>);
      setFiles((result.files ?? []) as TrashFile[]);
    } catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const permanentMeeting = async (id: string) => {
    if (!window.confirm(t("trash.confirm_meeting"))) return;
    try { await api.permanentlyDeleteMeeting(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };
  const permanentProject = async (id: string) => {
    if (!window.confirm(t("trash.confirm_project"))) return;
    try { await api.permanentlyDeleteProject(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };
  const permanentFile = async (file: TrashFile) => {
    if (!window.confirm(t("trash.confirm_file", { name: file.name }))) return;
    try { await api.permanentlyDeleteProjectFile(file.id); await load(); }
    catch (e) { setError((e as Error).message); }
  };

  if (loading) return <div className="panel"><Spinner label={t("trash.loading")} /></div>;
  return <div className="panel">
    <div className="head-row"><h1>{t("trash.title")}</h1><button className="btn" onClick={() => void load()}>{t("common.refresh")}</button></div>
    {error && <Note kind="error">{error}</Note>}
    {!meetings.length && !projects.length && !files.length ? <Note>{t("trash.empty")}</Note> : <>
    <div className="card"><h2>{t("trash.meetings")}</h2>
      {!meetings.length ? <p className="dim">{t("trash.no_meetings")}</p> : meetings.map((m) => <div className="trash-row" key={m.id}><div><strong>{m.title}</strong><div className="dim">{t("trash.deleted", { date: fmtDate(m.deleted_at) })}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreMeeting(m.id).then(load).catch((e) => setError((e as Error).message))}>{t("trash.restore")}</button><button className="btn small danger" onClick={() => void permanentMeeting(m.id)}>{t("trash.delete_permanently")}</button></div></div>)}
    </div>
    <div className="card"><h2>{t("trash.projects")}</h2>
      {!projects.length ? <p className="dim">{t("trash.no_projects")}</p> : projects.map((p) => <div className="trash-row" key={p.id}><div><strong>{p.name}</strong><div className="dim">{t("trash.deleted", { date: fmtDate(p.deleted_at) })}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreProject(p.id).then(load).catch((e) => setError((e as Error).message))}>{t("trash.restore")}</button><button className="btn small danger" onClick={() => void permanentProject(p.id)}>{t("trash.delete_permanently")}</button></div></div>)}
    </div>
    <div className="card"><h2>{t("trash.project_files")}</h2>
      {!files.length ? <p className="dim">{t("trash.no_project_files")}</p> : files.map((file) => <div className="trash-row" key={file.id}><div><strong>{file.name}</strong><div className="dim">{t("trash.file_meta", { project: file.project_name, date: fmtDate(file.deleted_at) })}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreProjectFile(file.id).then(load).catch((e) => setError((e as Error).message))}>{t("trash.restore")}</button><button className="btn small danger" onClick={() => void permanentFile(file)}>{t("trash.delete_permanently")}</button></div></div>)}
    </div>
    </>}
  </div>;
}
