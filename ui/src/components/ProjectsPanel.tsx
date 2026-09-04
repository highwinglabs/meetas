import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { ChatTurn, ModelSpec, Project, RagAnswer, Task } from "../types";
import { fmtDate } from "../format";
import { Note } from "./ui";

type ProjectDetail = Project & {
  meetings: Array<{ id: string; title: string; status: string; start_at: string | null; duration_s: number | null }>;
  files: Array<{ id: string; name: string; kind: string; size: number; created_at: string | null; extraction_status?: string; extraction_error?: string | null; extracted_chars?: number; indexed_at?: string | null; chunks?: number }>;
};

type ProjectChatMessage = { question: string; answer: RagAnswer };
type ProjectView = "overview" | "chat" | "meetings" | "tasks" | "files";
type ProjectTaskFilter = "all" | "active" | "archived" | "trash";

export default function ProjectsPanel({ onOpenMeeting }: { onOpenMeeting: (id: string, segmentId?: string) => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<ProjectDetail | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [projectModels, setProjectModels] = useState<ModelSpec[]>([]);
  const [projectChatModel, setProjectChatModel] = useState("");
  const [projectChatQuestion, setProjectChatQuestion] = useState("");
  const [projectChatMessages, setProjectChatMessages] = useState<ProjectChatMessage[]>([]);
  const [projectChatBusy, setProjectChatBusy] = useState(false);
  const [projectChatHydrated, setProjectChatHydrated] = useState(false);
  const [fileBusy, setFileBusy] = useState(false);
  const [projectView, setProjectView] = useState<ProjectView>("overview");
  const [projectTasks, setProjectTasks] = useState<Task[]>([]);
  const [projectTaskFilter, setProjectTaskFilter] = useState<ProjectTaskFilter>("all");

  const fileStatusLabel = (file: ProjectDetail["files"][number]) => {
    if (file.kind !== "document") return "Meeting erstellt";
    if (file.extraction_status === "ready") return `Bereit · ${file.chunks ?? 0} Abschnitte`;
    if (file.extraction_status === "failed") return file.extraction_error || "Nicht lesbar";
    return "Wird vorbereitet";
  };
  const taskStatusLabel = (task: Task) => {
    if (task.deleted_at) return "Papierkorb";
    if (task.archived_at) return "Archiv";
    return ({ offen: "Offen", laeuft: "Läuft", erledigt: "Erledigt" } as Record<string, string>)[task.status] ?? task.status;
  };
  const taskBucket = (task: Task): Exclude<ProjectTaskFilter, "all"> => task.deleted_at ? "trash" : task.archived_at ? "archived" : "active";

  const load = useCallback(() => api.projects(showArchived).then(setProjects).catch((e) => setError((e as Error).message)), [showArchived]);
  useEffect(() => {
    void load();
    api.modelCatalog().then((catalog) => {
      const installed = catalog.filter((model) => model.kind === "llm" && model.installed);
      setProjectModels(installed);
      setProjectChatModel((current) => current && installed.some((model) => model.id === current)
        ? current : (installed[0]?.id ?? ""));
    }).catch(() => setProjectModels([]));
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setProjectChatHydrated(false);
      setProjectChatMessages([]);
      return;
    }
    setProjectChatHydrated(false);
    setProjectChatMessages([]);
    try {
      const raw = localStorage.getItem(`project-chat:${selected.id}`);
      const parsed = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) {
        setProjectChatMessages(parsed.filter((entry) => entry && typeof entry.question === "string" && entry.answer));
      }
    } catch {
      // A damaged or unavailable browser cache must never block project work.
    }
    setProjectChatHydrated(true);
  }, [selected?.id]);

  useEffect(() => {
    if (!selected || !projectChatHydrated) return;
    try { localStorage.setItem(`project-chat:${selected.id}`, JSON.stringify(projectChatMessages)); }
    catch { /* Chat history is optional and must never block the project view. */ }
  }, [selected?.id, projectChatMessages, projectChatHydrated]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createProject(name, description);
      setName("");
      setDescription("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const open = async (id: string) => {
    try {
      const detail = await api.projectDetail(id) as unknown as ProjectDetail;
      const tasks = await api.listTasks(null, null, null, id, "all").catch(() => [] as Task[]);
      if (selected?.id !== id) setProjectView("overview");
      setSelected(detail);
      setProjectTasks(tasks);
      setProjectTaskFilter("all");
      setEditName(detail.name);
      setEditDescription(detail.description || "");
    } catch (e) {
      setError((e as Error).message);
      setProjectTasks([]);
    }
  };

  const visibleProjectTasks = projectTaskFilter === "all"
    ? projectTasks
    : projectTasks.filter((task) => taskBucket(task) === projectTaskFilter);
  const projectTaskCounts = {
    all: projectTasks.length,
    active: projectTasks.filter((task) => taskBucket(task) === "active").length,
    archived: projectTasks.filter((task) => taskBucket(task) === "archived").length,
    trash: projectTasks.filter((task) => taskBucket(task) === "trash").length,
  };

  const save = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.updateProject(selected.id, { name: editName, description: editDescription });
      await load();
      await open(selected.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.updateProject(selected.id, { status: selected.status === "archived" ? "active" : "archived" });
      setSelected(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const trash = async () => {
    if (!selected || !window.confirm("Projekt in den Papierkorb verschieben? Archivieren und Papierkorb sind getrennt.")) return;
    setBusy(true); setError(null);
    try { await api.trashProject(selected.id); setSelected(null); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  const askProject = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !projectChatQuestion.trim()) return;
    if (!projectChatModel) {
      setError("Für Projektfragen muss ein installiertes KI-Modell ausgewählt sein.");
      return;
    }
    const question = projectChatQuestion.trim();
    setProjectChatBusy(true);
    setError(null);
    try {
      const history: ChatTurn[] = projectChatMessages.flatMap((message) => [
        { role: "user" as const, content: message.question },
        { role: "assistant" as const, content: message.answer.answer },
      ]).slice(-12);
      const answer = await api.chat(question, {
        project_id: selected.id,
        model: projectChatModel,
        limit: 25,
        history,
      });
      setProjectChatMessages((current) => [...current, { question, answer }]);
      setProjectChatQuestion("");
    } catch (e) { setError((e as Error).message); }
    finally { setProjectChatBusy(false); }
  };

  const deleteProjectChatMessage = (index: number) => {
    if (!window.confirm("Diesen Chat löschen?")) return;
    setProjectChatMessages((current) => current.filter((_, messageIndex) => messageIndex !== index));
  };

  const uploadProjectFile = async (file?: File) => {
    if (!selected || !file) return;
    setFileBusy(true); setError(null);
    try {
      await api.upload(file, { project_id: selected.id });
      await open(selected.id);
    } catch (e) { setError((e as Error).message); }
    finally { setFileBusy(false); }
  };

  const reindexFiles = async () => {
    if (!selected) return;
    setFileBusy(true); setError(null);
    try {
      await api.reindexProjectFiles(selected.id);
      await open(selected.id);
    } catch (e) { setError((e as Error).message); }
    finally { setFileBusy(false); }
  };

  const trashFile = async (fileId: string, fileName: string) => {
    if (!selected || !window.confirm(`„${fileName}“ in den Papierkorb verschieben?`)) return;
    setFileBusy(true); setError(null);
    try { await api.trashProjectFile(fileId); await open(selected.id); }
    catch (e) { setError((e as Error).message); }
    finally { setFileBusy(false); }
  };

  return (
    <div className="panel">
      <h1>Projekte</h1>
      {error && <Note kind="error">{error}</Note>}
      <div className="card">
        <h2>Neues Projekt</h2>
        <div className="row wrap">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="z. B. Prüfung Kunde A" />
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Beschreibung (optional)" />
          <button className="btn primary" onClick={() => void create()} disabled={busy || !name.trim()}>Anlegen</button>
        </div>
      </div>
      <div className="row wrap project-list-tools">
        <label className="check"><input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> Archivierte Projekte anzeigen</label>
      </div>
      <div className="project-list">
        {projects.length === 0 && !error && <Note>Keine Projekte vorhanden.</Note>}
        {projects.map((project) => (
          <div className="project-card" key={project.id}>
            <button className="project-item" onClick={() => selected?.id === project.id ? setSelected(null) : void open(project.id)} aria-expanded={selected?.id === project.id}>
              <span><strong>{project.name}</strong><span className="project-desc">{project.description || "Keine Beschreibung"}</span></span>
              <span className="project-card-meta"><span className="dim">{project.meetings} Meetings · {project.files} Dateien</span><span className="project-open">{selected?.id === project.id ? "Geöffnet" : "Öffnen"} <span aria-hidden="true">↗</span></span></span>
            </button>
            {selected?.id === project.id && (
              <div className="project-workspace">
                <div className="project-workspace-head">
                  <div>
                    <div className="detail-meta">
                      <span className="chip">{selected.status === "archived" ? "Archiviert" : "Aktiv"}</span>
                      <span>{selected.meetings.length} Meetings</span>
                      <span>{selected.files.length} Dateien</span>
                    </div>
                  </div>
                  <button className="btn small" onClick={() => setSelected(null)}>Zuklappen</button>
                </div>
                <nav className="project-tabs" aria-label="Projektbereiche">
                  <button className={projectView === "overview" ? "tab active" : "tab"} onClick={() => setProjectView("overview")}>Übersicht</button>
                  <button className={projectView === "chat" ? "tab active" : "tab"} onClick={() => setProjectView("chat")}>KI fragen</button>
                  <button className={projectView === "meetings" ? "tab active" : "tab"} onClick={() => setProjectView("meetings")}>Meetings <span className="count">{selected.meetings.length}</span></button>
                  <button className={projectView === "tasks" ? "tab active" : "tab"} onClick={() => setProjectView("tasks")}>Aufgaben <span className="count">{projectTasks.length}</span></button>
                  <button className={projectView === "files" ? "tab active" : "tab"} onClick={() => setProjectView("files")}>Dateien <span className="count">{selected.files.length}</span></button>
                </nav>
                {projectView === "overview" && <div className="card project-section">
                  <div className="settings-grid">
                    <label className="field"><span>Name</span><input value={editName} onChange={(e) => setEditName(e.target.value)} /></label>
                    <label className="field"><span>Beschreibung</span><input value={editDescription} onChange={(e) => setEditDescription(e.target.value)} /></label>
                  </div>
                  <div className="row wrap">
                    <button className="btn primary" onClick={() => void save()} disabled={busy || !editName.trim()}>Speichern</button>
                    <button className="btn" onClick={() => void archive()} disabled={busy}>{selected.status === "archived" ? "Wieder aktivieren" : "Archivieren"}</button>
                    <button className="btn danger" onClick={() => void trash()} disabled={busy}>Papierkorb</button>
                    <span className="dim">{selected.status === "archived" ? "Archiviert" : "Aktiv"}</span>
                  </div>
                </div>}
                {projectView === "chat" && <div className="card project-section project-chat-card">
                  {projectChatMessages.length > 0 && <div className="project-chat-history">
                    <div className="row spread chat-history-head"><strong>Gesprächsverlauf</strong><button className="btn small" type="button" onClick={() => { if (window.confirm("Gesprächsverlauf dieses Projekts löschen?")) setProjectChatMessages([]); }}>Verlauf löschen</button></div>
                    {projectChatMessages.map((message, index) => <details className="project-chat-turn chat-turn-collapsible" key={`${selected.id}-${index}`}>
                      <summary className="chat-question"><strong>Du</strong><span>{message.question}</span></summary>
                      <div className="chat-turn-actions"><button className="btn small danger" type="button" onClick={() => deleteProjectChatMessage(index)}>Chat löschen</button></div>
                      <div className="rag"><div className={"note " + (message.answer.grounded ? "ok" : "warn")}>{message.answer.grounded ? "Mit Quellen aus diesem Projekt" : "Keine ausreichende Information in den Projektinhalten gefunden."}</div><p className="rag-answer">{message.answer.answer}</p>{message.answer.sources.length > 0 && <div className="project-chat-sources"><span className="dim">Quellen</span>{message.answer.sources.slice(0, 5).map((source) => source.meeting_id ? <button className="link" key={`${source.meeting_id}-${source.segment_id}`} onClick={() => onOpenMeeting(source.meeting_id!, source.segment_id)}>{source.meeting_title ?? "Meeting"} · {source.timestamp}</button> : source.file_id ? <a className="link" key={`${source.file_id}-${source.segment_id}`} href={api.projectFileUrl(source.file_id)} download={source.file_name ?? undefined}>{source.file_name ?? "Datei"}{source.locator ? ` · ${source.locator}` : ""} öffnen →</a> : null)}</div>}</div>
                    </details>)}
                  </div>}
                  <form className="project-chat-form" onSubmit={(event) => void askProject(event)}>
                    <input className="grow" value={projectChatQuestion} onChange={(event) => setProjectChatQuestion(event.target.value)} placeholder="Frage zu diesem Projekt stellen…" aria-label="Frage zu diesem Projekt" />
                    {projectModels.length > 0 && <label className="inline-select"><span>Modell</span><select value={projectChatModel} onChange={(event) => setProjectChatModel(event.target.value)} aria-label="KI-Modell">{projectModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>}
                    <button className="btn primary" type="submit" disabled={projectChatBusy || !projectChatQuestion.trim() || !projectChatModel}>{projectChatBusy ? "Beantworte…" : "Fragen"}</button>
                  </form>
                  {!projectModels.length && <p className="hint">Kein installiertes KI-Modell verfügbar.</p>}
                </div>}
                {projectView === "meetings" && <div className="card project-section">
                  {selected.meetings.length === 0 && <p className="dim">Keine Meetings.</p>}
                  {selected.meetings.map((meeting) => (
                    <button className="project-meeting" key={meeting.id} onClick={() => onOpenMeeting(meeting.id)}>
                      <strong>{meeting.title}</strong><span className="dim">{meeting.start_at ? fmtDate(meeting.start_at) : "–"}</span>
                    </button>
                  ))}
                </div>}
                {projectView === "tasks" && <div className="card project-section">
                  <div className="project-task-filters" aria-label="Projektaufgaben filtern">
                    {(["all", "active", "archived", "trash"] as const).map((filter) => <button type="button" key={filter} className={projectTaskFilter === filter ? "chip active" : "chip"} onClick={() => setProjectTaskFilter(filter)} aria-pressed={projectTaskFilter === filter}>{filter === "all" ? "Alle" : filter === "active" ? "Aktiv" : filter === "archived" ? "Archiv" : "Papierkorb"} <span className="count">{projectTaskCounts[filter]}</span></button>)}
                  </div>
                  {projectTasks.length === 0 ? <p className="dim">Keine Aufgaben in diesem Projekt.</p> : visibleProjectTasks.length === 0 ? <p className="dim">Keine Aufgaben in diesem Bereich.</p> : <div className="project-task-list">
                    {visibleProjectTasks.map((task) => <div className="project-task" key={task.id}>
                      <div className="project-task-main"><strong>{task.text}</strong><div className="task-sub">{task.meeting_id && task.meeting_title ? <button type="button" className="task-link" onClick={() => onOpenMeeting(task.meeting_id!)}>{task.meeting_title}</button> : <span className="dim">Ohne Meeting</span>}{task.owner ? <span className="dim">{task.owner}</span> : null}{task.deadline && task.deadline !== "nicht angegeben" ? <span className="dim">Frist: {task.deadline}</span> : null}</div></div>
                      <span className={task.deleted_at ? "badge failed" : task.archived_at ? "badge warn" : task.status === "erledigt" ? "badge done" : "badge"}>{taskStatusLabel(task)}</span>
                    </div>)}
                  </div>}
                </div>}
                {projectView === "files" && <div className="card project-section">
                  <div className="row wrap project-file-tools">
                    <label className="btn upload-button">{fileBusy ? "Bereite Datei vor…" : "Datei hinzufügen"}<input type="file" hidden accept=".pdf,.docx,.odt,.xlsx,.pptx,.txt,.md,.csv" disabled={fileBusy} onChange={(event) => { void uploadProjectFile(event.target.files?.[0]); event.currentTarget.value = ""; }} /></label>
                    <button className="btn" onClick={() => void reindexFiles()} disabled={fileBusy || selected.files.length === 0}>Neu vorbereiten</button>
                  </div>
                  {selected.files.length === 0 && <p className="dim">Keine Dateien.</p>}
                  {selected.files.map((file) => <div className="project-file" key={file.id}><span><strong>{file.name}</strong><span className="dim">{file.kind} · {file.size} Bytes</span></span><span className={file.extraction_status === "ready" || file.kind !== "document" ? "model-ready" : file.extraction_status === "failed" ? "model-missing" : "dim"}>{fileStatusLabel(file)}</span><span className="row"><a className="btn small" href={api.projectFileUrl(file.id)} download={file.name}>Herunterladen</a><button className="btn small danger" disabled={fileBusy} onClick={() => void trashFile(file.id, file.name)}>Papierkorb</button></span></div>)}
                </div>}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
