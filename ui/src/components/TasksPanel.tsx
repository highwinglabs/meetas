import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem, Project, Task, TaskOverview } from "../types";
import { Note, Spinner } from "./ui";
import { useI18n, type MessageKey } from "../i18n";

const TASK_STATUS: { value: string; msgKey: MessageKey }[] = [
  { value: "offen", msgKey: "task.status.open" },
  { value: "laeuft", msgKey: "task.status.in_progress" },
  { value: "erledigt", msgKey: "task.status.done" },
  { value: "ueberfaellig", msgKey: "task.status.overdue" },
  { value: "ohne_deadline", msgKey: "task.status.no_deadline" },
];
export default function TasksPanel({ onOpenMeeting }: { onOpenMeeting: (id: string) => void }) {
  const { t } = useI18n();
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
    if (action === "trash" && !window.confirm(t("task.confirm_trash"))) return;
    if (action === "permanent" && !window.confirm(t("task.confirm_permanent"))) return;
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
    return <div className="panel"><Spinner label={t("task.loading")} /></div>;
  }

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow">
          <h1 className="detail-title">{t("tasks.title")}</h1>
          {overview && (
            <div className="detail-meta">
              <span>{t("task.overview_open", { n: overview.open })}</span>
              <span>{t("task.overview_done", { n: overview.by_status["erledigt"] ?? 0 })}</span>
              <span>{t("task.overview_total", { n: overview.total })}</span>
            </div>
          )}
        </div>
      </div>

      {error && <Note kind="error">{error}</Note>}

      <div className="card">
        <div className="row wrap">
          <input className="grow" value={newText} onChange={(e) => setNewText(e.target.value)} placeholder={t("task.new_placeholder")} />
          <select value={newProject} onChange={(e) => setNewProject(e.target.value)}><option value="">{t("common.no_project")}</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
          <select value={newMeeting} onChange={(e) => setNewMeeting(e.target.value)}><option value="">{t("common.no_meeting")}</option>{meetings.map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}</select>
          <button className="btn primary" onClick={() => void create()} disabled={creating || !newText.trim()}>{creating ? t("task.creating") : t("task.create")}</button>
        </div>
      </div>

      <div className="task-view-switcher" aria-label={t("task.view_aria")}>
        <button className={viewFilter === "active" ? "tab active" : "tab"} onClick={() => setViewFilter("active")} aria-pressed={viewFilter === "active"}>{t("common.active")}</button>
        <button className={viewFilter === "archived" ? "tab active" : "tab"} onClick={() => setViewFilter("archived")} aria-pressed={viewFilter === "archived"}>{t("common.archive")}</button>
        <button className={viewFilter === "trash" ? "tab active" : "tab"} onClick={() => setViewFilter("trash")} aria-pressed={viewFilter === "trash"}>{t("common.trash")}</button>
      </div>

      <details className="card task-filters">
        <summary>{t("task.filters")}{activeFilterCount > 0 ? ` · ${t("task.filters_active", { n: activeFilterCount })}` : ""}</summary>
        <div className="row wrap filters">
          <label className="field">
            <span>{t("common.status")}</span>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {TASK_STATUS.map((s) => <option key={s.value} value={s.value}>{t(s.msgKey)}</option>)}
            </select>
          </label>
          <label className="field">
            <span>{t("task.owner")}</span>
            <select value={ownerFilter} onChange={(e) => setOwnerFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {owners.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
          <label className="field">
            <span>{t("common.project")}</span>
            <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <label className="field">
            <span>{t("common.meeting")}</span>
            <select value={meetingFilter} onChange={(e) => setMeetingFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {meetings.map((meeting) => <option key={meeting.id} value={meeting.id}>{meeting.title}</option>)}
            </select>
          </label>
        </div>
      </details>

      {!tasks.length ? (
        <Note>{t("task.empty_none")}{statusFilter || ownerFilter || projectFilter || meetingFilter ? t("task.empty_for_filter") : ""}.</Note>
      ) : (
        <div className="task-list">
          {tasks.map((taskItem) => (
            <div key={taskItem.id} className={"task " + (taskItem.display_status ?? taskItem.status)}>
              <div className="task-main">
                <input
                  className="task-text task-edit"
                  defaultValue={taskItem.text}
                  aria-label={t("task.edit_aria", { text: taskItem.text })}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v && v !== taskItem.text) setText(taskItem, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
                <div className="task-sub">
                  <span className="chip">{taskItem.source ?? t("task.source_manual")}</span>
                  {taskItem.meeting_id && taskItem.meeting_title ? (
                    <button type="button" className="task-link" onClick={() => onOpenMeeting(taskItem.meeting_id!)}>{taskItem.meeting_title}</button>
                  ) : (
                    <span className="dim">{t("task.no_meeting")}</span>
                  )}
                  {taskItem.project_name ? <span>{t("task.project_label", { name: taskItem.project_name })}</span> : null}
                  {taskItem.deadline && taskItem.deadline !== "nicht angegeben" ? <span>{t("task.deadline_label", { deadline: taskItem.deadline })}</span> : null}
                </div>
                <input
                  className="task-deadline task-edit"
                  defaultValue={taskItem.deadline === "nicht angegeben" ? "" : (taskItem.deadline ?? "")}
                  placeholder={t("task.deadline_example")}
                  aria-label={t("task.deadline")}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    const old = taskItem.deadline === "nicht angegeben" ? "" : (taskItem.deadline ?? "");
                    if (v !== old) setDeadline(taskItem, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
              </div>
              <div className="task-controls">
                <select
                  className="task-status"
                  value={taskItem.status}
                  onChange={(e) => setStatus(taskItem, e.target.value)}
                  title={t("common.status")}
                >
                  {TASK_STATUS.map((s) => <option key={s.value} value={s.value}>{t(s.msgKey)}</option>)}
                </select>
                <input
                  className="task-owner"
                  defaultValue={taskItem.owner ?? ""}
                  placeholder={t("task.owner")}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v !== (taskItem.owner ?? "")) setOwner(taskItem, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                />
                <div className="row task-actions">
                  {viewFilter !== "active" && <button className="btn small" onClick={() => void lifecycle("restore", taskItem)}>{t("common.restore")}</button>}
                  <details className="task-actions-menu">
                    <summary className="btn small" aria-label={t("task.more_aria")}>{t("task.more")}</summary>
                    <div className="task-actions-menu-popover">
                      {viewFilter === "active" && <button className="btn small" onClick={() => void lifecycle("archive", taskItem)}>{t("task.archive")}</button>}
                      {viewFilter !== "trash" && <button className="btn small danger" onClick={() => void lifecycle("trash", taskItem)}>{t("task.trash")}</button>}
                      {viewFilter === "trash" && <button className="btn small danger" onClick={() => void lifecycle("permanent", taskItem)}>{t("common.delete_permanent")}</button>}
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
