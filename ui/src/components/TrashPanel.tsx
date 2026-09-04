import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem, Project } from "../types";
import { fmtDate } from "../format";
import { Note, Spinner } from "./ui";

type TrashMeeting = MeetingListItem & { deleted_at?: string | null };
type TrashFile = { id: string; name: string; project_id: string; project_name: string; deleted_at?: string | null };

export default function TrashPanel() {
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
    if (!window.confirm("Endgültig löschen? Aufnahme, Transkript und Analyse werden entfernt.")) return;
    try { await api.permanentlyDeleteMeeting(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };
  const permanentProject = async (id: string) => {
    if (!window.confirm("Projekt endgültig löschen? Zugehörige Projektdateien werden entfernt; Aufnahmen der Meetings bleiben erhalten.")) return;
    try { await api.permanentlyDeleteProject(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };
  const permanentFile = async (file: TrashFile) => {
    if (!window.confirm(`„${file.name}“ endgültig löschen? Die Projektdatei und ihre lokale Indexierung werden entfernt; eine zugehörige Meeting-Aufnahme bleibt erhalten.`)) return;
    try { await api.permanentlyDeleteProjectFile(file.id); await load(); }
    catch (e) { setError((e as Error).message); }
  };

  if (loading) return <div className="panel"><Spinner label="Lade Papierkorb…" /></div>;
  return <div className="panel">
    <div className="head-row"><h1>Papierkorb</h1><button className="btn" onClick={() => void load()}>Aktualisieren</button></div>
    {error && <Note kind="error">{error}</Note>}
    <div className="card"><h2>Meetings</h2>
      {!meetings.length ? <p className="dim">Keine gelöschten Meetings.</p> : meetings.map((m) => <div className="trash-row" key={m.id}><div><strong>{m.title}</strong><div className="dim">gelöscht {fmtDate(m.deleted_at)}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreMeeting(m.id).then(load).catch((e) => setError((e as Error).message))}>Wiederherstellen</button><button className="btn small danger" onClick={() => void permanentMeeting(m.id)}>Endgültig löschen</button></div></div>)}
    </div>
    <div className="card"><h2>Projekte</h2>
      {!projects.length ? <p className="dim">Keine gelöschten Projekte.</p> : projects.map((p) => <div className="trash-row" key={p.id}><div><strong>{p.name}</strong><div className="dim">gelöscht {fmtDate(p.deleted_at)}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreProject(p.id).then(load).catch((e) => setError((e as Error).message))}>Wiederherstellen</button><button className="btn small danger" onClick={() => void permanentProject(p.id)}>Endgültig löschen</button></div></div>)}
    </div>
    <div className="card"><h2>Projektdateien</h2>
      {!files.length ? <p className="dim">Keine gelöschten Projektdateien.</p> : files.map((file) => <div className="trash-row" key={file.id}><div><strong>{file.name}</strong><div className="dim">{file.project_name} · gelöscht {fmtDate(file.deleted_at)}</div></div><div className="row"><button className="btn small" onClick={() => api.restoreProjectFile(file.id).then(load).catch((e) => setError((e as Error).message))}>Wiederherstellen</button><button className="btn small danger" onClick={() => void permanentFile(file)}>Endgültig löschen</button></div></div>)}
    </div>
  </div>;
}
