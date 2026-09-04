import type { Task } from "../types";
import { Note } from "./ui";

const TASK_STATUS: { value: string; label: string }[] = [
  { value: "offen", label: "Offen" },
  { value: "laeuft", label: "Läuft" },
  { value: "erledigt", label: "Erledigt" },
];

export type TaskUpdateValues = { status?: string; owner?: string; text?: string; deadline?: string };

export default function TasksTab({
  tasks,
  taskText,
  taskOwner,
  taskDeadline,
  onTaskTextChange,
  onTaskOwnerChange,
  onTaskDeadlineChange,
  creating,
  onCreate,
  extracting,
  canExtract,
  onExtract,
  autoNote,
  onUpdate,
  onLifecycle,
}: {
  tasks: Task[];
  taskText: string;
  taskOwner: string;
  taskDeadline: string;
  onTaskTextChange: (value: string) => void;
  onTaskOwnerChange: (value: string) => void;
  onTaskDeadlineChange: (value: string) => void;
  creating: boolean;
  onCreate: () => void;
  extracting: boolean;
  canExtract: boolean;
  onExtract: () => void;
  autoNote: string | null;
  onUpdate: (task: Task, values: TaskUpdateValues) => void;
  onLifecycle: (action: "archive" | "trash", task: Task) => void;
}) {
  return (
    <>
      <div className="card actions">
        <div className="row wrap">
          <input className="grow" value={taskText} onChange={(e) => onTaskTextChange(e.target.value)} placeholder="Was soll erledigt werden?" />
          <input value={taskOwner} onChange={(e) => onTaskOwnerChange(e.target.value)} placeholder="Verantwortlich (optional)" />
          <input value={taskDeadline} onChange={(e) => onTaskDeadlineChange(e.target.value)} placeholder="Frist (optional)" />
          <button className="btn primary" onClick={onCreate} disabled={creating || !taskText.trim()}>{creating ? "Lege an…" : "Aufgabe anlegen"}</button>
        </div>
      </div>
      <div className="card actions">
        <div className="row spread"><div className="actions-title">Aufgaben aus diesem Meeting</div><button className="btn" onClick={onExtract} disabled={extracting || !canExtract}>{extracting ? "Extrahiere…" : "Aus KI-Analyse übernehmen"}</button></div>
        {autoNote && <Note kind="ok">{autoNote}</Note>}
      </div>
      {!tasks.length ? <Note>Keine Aufgaben für dieses Meeting.</Note> : <div className="task-list">
        {tasks.map((task) => <div key={task.id} className={"task " + (task.display_status ?? task.status)}>
          <div className="task-main">
            <input className="task-text task-edit" defaultValue={task.text} aria-label={`Aufgabe bearbeiten: ${task.text}`} onBlur={(e) => { const value = e.target.value.trim(); if (value && value !== task.text) onUpdate(task, { text: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
            <div className="task-sub"><span className="chip">{task.source ?? "manuell"}</span>{task.source_segment_id ? <span className="dim">Aus Transkript abgeleitet</span> : <span className="dim">Manuell angelegt</span>}</div>
            <input className="task-deadline task-edit" defaultValue={task.deadline === "nicht angegeben" ? "" : (task.deadline ?? "")} placeholder="Deadline (optional)" aria-label="Deadline" onBlur={(e) => { const value = e.target.value.trim(); const old = task.deadline === "nicht angegeben" ? "" : (task.deadline ?? ""); if (value !== old) onUpdate(task, { deadline: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
          </div>
          <div className="task-controls"><select className="task-status" value={task.status} onChange={(e) => onUpdate(task, { status: e.target.value })} title="Status">{TASK_STATUS.map((status) => <option key={status.value} value={status.value}>{status.label}</option>)}</select><input className="task-owner" defaultValue={task.owner ?? ""} placeholder="Verantwortlich" aria-label="Verantwortlich" onBlur={(e) => { const value = e.target.value.trim(); if (value !== (task.owner ?? "")) onUpdate(task, { owner: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} /><div className="row task-actions"><details className="task-actions-menu"><summary className="btn small" aria-label="Weitere Aufgabenaktionen">Mehr</summary><div className="task-actions-menu-popover"><button className="btn small" onClick={() => onLifecycle("archive", task)}>Archivieren</button><button className="btn small danger" onClick={() => onLifecycle("trash", task)}>In Papierkorb</button></div></details></div></div>
        </div>)}
      </div>}
    </>
  );
}
