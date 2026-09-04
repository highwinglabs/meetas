import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem, Project, Task, TaskOverview } from "../types";
import { Note, Spinner } from "./ui";

const TASK_STATUS: { value: string; label: string }[] = [
  { value: "offen", label: "Offen" },
  { value: "laeuft", label: "Läuft" },
  { value: "erledigt", label: "Erledigt" },
  { value: "ueberfaellig", label: "Überfällig" },
  { value: "ohne_deadline", label: "Ohne Deadline" },
];
export default function TasksPanel({ onOpenMeeting }: { onOpenMeeting: (id: string) => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [overview, setOverview] = useState<TaskOverview | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [meetingFilter, setMeetingFilter] = useState("");
  const [viewFilter, setViewFilter] = useState("active");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [projects, setProjects] = useState<Project[]>([]);
  const [meetings, setMeetings] = useState<MeetingListItem[]>([]);
  const [newText, setNewText] = useState("");
  const [newProject, setNewProject] = useState("");
  const [newMeeting, setNewMeeting] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [t, o] = await Promise.all([
        api.listTasks(statusFilter || null, ownerFilter || null, meetingFilter || null, projectFilter || null, viewFilter),
        api.taskOverview(viewFilter),
      ]);
      setTasks(t);
      setOverview(o);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, ownerFilter, projectFilter, meetingFilter, viewFilter]);

  useEffect(() => { load(); void Promise.all([api.projects(), api.listMeetings()]).then(([p, m]) => { setProjects(p); setMeetings(m); }).catch(() => {}); }, [load]);

  const owners = Array.from(new Set(tasks.map((t) => t.owner).filter(Boolean))) as string[];
  const activeFilterCount = [statusFilter, ownerFilter, projectFilter, meetingFilter].filter(Boolean).length;

  const setStatus = async (t: Task, status: string) => {
    setError(null);
    try {
      await api.updateTask(t.id, { status });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setOwner = async (t: Task, owner: string) => {
    setError(null);
    try {
      await api.updateTask(t.id, { owner });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setText = async (t: Task, text: string) => {
    setError(null);
    try {
      await api.updateTask(t.id, { text });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setDeadline = async (t: Task, deadline: string) => {
    setError(null);
    try {
      await api.updateTask(t.id, { deadline });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const create = async () => {
    if (!newText.trim()) return;
    setCreating(true);
    try {
      await api.createTask({ text: newText.trim(), project_id: newProject || null, meeting_id: newMeeting || null });
      setNewText(""); setNewProject(""); setNewMeeting(""); await load();
    } catch (e) { setError((e as Error).message); }
    finally { setCreating(false); }
  };

  const lifecycle = async (action: "archive" | "restore" | "trash" | "permanent", task: Task) => {
    if (action === "trash" && !window.confirm("Aufgabe in den Papierkorb verschieben? Sie kann wiederhergestellt werden.")) return;
    if (action === "permanent" && !window.confirm("Aufgabe endgültig löschen? Dieser Vorgang kann nicht rückgängig gemacht werden.")) return;
    setError(null);
    try {
      if (action === "archive") await api.archiveTask(task.id);
      else if (action === "restore") await api.restoreTask(task.id);
      else if (action === "trash") await api.trashTask(task.id);
      else await api.permanentlyDeleteTask(task.id);
      await load();
    } catch (e) { setError((e as Error).message); }
  };

  if (loading) {
    return <div className="panel"><Spinner label="Lade Aufgaben…" /></div>;
  }

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow">
          <h1 className="detail-title">Aufgaben</h1>
          {overview && (
            <div className="detail-meta">
              <span>{overview.open} offen</span>
              <span>{overview.by_status["erledigt"] ?? 0} erledigt</span>
              <span>{overview.total} gesamt</span>
            </div>
          )}
        </div>
      </div>

      {error && <Note kind="error">{error}</Note>}

      <div className="card">
        <div className="row wrap">
          <input className="grow" value={newText} onChange={(e) => setNewText(e.target.value)} placeholder="Was soll erledigt werden?" />
          <select value={newProject} onChange={(e) => setNewProject(e.target.value)}><option value="">Kein Projekt</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
          <select value={newMeeting} onChange={(e) => setNewMeeting(e.target.value)}><option value="">Kein Meeting</option>{meetings.map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}</select>
          <button className="btn primary" onClick={() => void create()} disabled={creating || !newText.trim()}>{creating ? "Lege an…" : "Aufgabe anlegen"}</button>
        </div>
      </div>

      <div className="task-view-switcher" aria-label="Aufgabenansicht">
        <button className={viewFilter === "active" ? "tab active" : "tab"} onClick={() => setViewFilter("active")} aria-pressed={viewFilter === "active"}>Aktiv</button>
        <button className={viewFilter === "archived" ? "tab active" : "tab"} onClick={() => setViewFilter("archived")} aria-pressed={viewFilter === "archived"}>Archiv</button>
        <button className={viewFilter === "trash" ? "tab active" : "tab"} onClick={() => setViewFilter("trash")} aria-pressed={viewFilter === "trash"}>Papierkorb</button>
      </div>

      <details className="card task-filters">
        <summary>Filter{activeFilterCount > 0 ? ` · ${activeFilterCount} aktiv` : ""}</summary>
        <div className="row wrap filters">
          <label className="field">
            <span>Status</span>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">Alle</option>
              {TASK_STATUS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Verantwortlich</span>
            <select value={ownerFilter} onChange={(e) => setOwnerFilter(e.target.value)}>
              <option value="">Alle</option>
              {owners.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Projekt</span>
            <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
              <option value="">Alle</option>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Meeting</span>
            <select value={meetingFilter} onChange={(e) => setMeetingFilter(e.target.value)}>
              <option value="">Alle</option>
              {meetings.map((meeting) => <option key={meeting.id} value={meeting.id}>{meeting.title}</option>)}
            </select>
          </label>
        </div>
      </details>

      {!tasks.length ? (
        <Note>Keine Aufgaben{statusFilter || ownerFilter || projectFilter || meetingFilter ? " für diesen Filter" : ""}.</Note>
      ) : (
        <div className="task-list">
          {tasks.map((t) => (
            <div key={t.id} className={"task " + (t.display_status ?? t.status)}>
              <div className="task-main">
                <input
                  className="task-text task-edit"
                  defaultValue={t.text}
                  aria-label={`Aufgabe bearbeiten: ${t.text}`}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v && v !== t.text) setText(t, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
                <div className="task-sub">
                  <span className="chip">{t.source ?? "manuell"}</span>
                  {t.meeting_id && t.meeting_title ? (
                    <button type="button" className="task-link" onClick={() => onOpenMeeting(t.meeting_id!)}>{t.meeting_title}</button>
                  ) : (
                    <span className="dim">ohne Meeting</span>
                  )}
                  {t.project_name ? <span>Projekt: {t.project_name}</span> : null}
                  {t.deadline && t.deadline !== "nicht angegeben" ? <span>Deadline: {t.deadline}</span> : null}
                </div>
                <input
                  className="task-deadline task-edit"
                  defaultValue={t.deadline === "nicht angegeben" ? "" : (t.deadline ?? "")}
                  placeholder="Deadline (z. B. Freitag oder 2026-09-04)"
                  aria-label="Deadline"
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    const old = t.deadline === "nicht angegeben" ? "" : (t.deadline ?? "");
                    if (v !== old) setDeadline(t, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
              </div>
              <div className="task-controls">
                <select
                  className="task-status"
                  value={t.status}
                  onChange={(e) => setStatus(t, e.target.value)}
                  title="Status"
                >
                  {TASK_STATUS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
                <input
                  className="task-owner"
                  defaultValue={t.owner ?? ""}
                  placeholder="Verantwortlich"
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v !== (t.owner ?? "")) setOwner(t, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
                <div className="row task-actions">
                  {viewFilter !== "active" && <button className="btn small" onClick={() => void lifecycle("restore", t)}>Wiederherstellen</button>}
                  <details className="task-actions-menu">
                    <summary className="btn small" aria-label="Weitere Aufgabenaktionen">Mehr</summary>
                    <div className="task-actions-menu-popover">
                      {viewFilter === "active" && <button className="btn small" onClick={() => void lifecycle("archive", t)}>Archivieren</button>}
                      {viewFilter !== "trash" && <button className="btn small danger" onClick={() => void lifecycle("trash", t)}>In Papierkorb</button>}
                      {viewFilter === "trash" && <button className="btn small danger" onClick={() => void lifecycle("permanent", t)}>Endgültig löschen</button>}
                    </div>
                  </details>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
