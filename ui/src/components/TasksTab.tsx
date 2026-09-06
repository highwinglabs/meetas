import type { Task } from "../types";
import { Note } from "./ui";
import { useI18n, type MessageKey } from "../i18n";

const TASK_STATUS: { value: string; msgKey: MessageKey }[] = [
  { value: "offen", msgKey: "task.status.open" },
  { value: "laeuft", msgKey: "task.status.in_progress" },
  { value: "erledigt", msgKey: "task.status.done" },
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
  const { t } = useI18n();
  return (
    <>
      <div className="card actions">
        <div className="row wrap">
          <input className="grow" value={taskText} onChange={(e) => onTaskTextChange(e.target.value)} placeholder={t("task.new_placeholder")} />
          <input value={taskOwner} onChange={(e) => onTaskOwnerChange(e.target.value)} placeholder={t("task.owner_optional")} />
          <input value={taskDeadline} onChange={(e) => onTaskDeadlineChange(e.target.value)} placeholder={t("task.deadline_optional")} />
          <button className="btn primary" onClick={onCreate} disabled={creating || !taskText.trim()}>{creating ? t("task.creating") : t("task.create")}</button>
        </div>
      </div>
      <div className="card actions">
        <div className="row spread"><div className="actions-title">{t("task.from_meeting")}</div><button className="btn" onClick={onExtract} disabled={extracting || !canExtract}>{extracting ? t("task.extracting") : t("task.extract")}</button></div>
        {autoNote && <Note kind="ok">{autoNote}</Note>}
      </div>
      {!tasks.length ? <Note>{t("task.empty")}</Note> : <div className="task-list">
        {tasks.map((task) => <div key={task.id} className={"task " + (task.display_status ?? task.status)}>
          <div className="task-main">
            <input className="task-text task-edit" defaultValue={task.text} aria-label={t("task.edit_aria", { text: task.text })} onBlur={(e) => { const value = e.target.value.trim(); if (value && value !== task.text) onUpdate(task, { text: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
            <div className="task-sub"><span className="chip">{task.source ?? t("task.source_manual")}</span></div>
            <input className="task-deadline task-edit" defaultValue={task.deadline === "nicht angegeben" ? "" : (task.deadline ?? "")} placeholder={t("task.deadline_placeholder")} aria-label={t("task.deadline")} onBlur={(e) => { const value = e.target.value.trim(); const old = task.deadline === "nicht angegeben" ? "" : (task.deadline ?? ""); if (value !== old) onUpdate(task, { deadline: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
          </div>
          <div className="task-controls"><select className="task-status" value={task.status} onChange={(e) => onUpdate(task, { status: e.target.value })} title={t("common.status")}>{TASK_STATUS.map((status) => <option key={status.value} value={status.value}>{t(status.msgKey)}</option>)}</select><input className="task-owner" defaultValue={task.owner ?? ""} placeholder={t("task.owner")} aria-label={t("task.owner")} onBlur={(e) => { const value = e.target.value.trim(); if (value !== (task.owner ?? "")) onUpdate(task, { owner: value }); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} /><div className="row task-actions"><details className="task-actions-menu"><summary className="btn small" aria-label={t("task.more_aria")}>{t("task.more")}</summary><div className="task-actions-menu-popover"><button className="btn small" onClick={() => onLifecycle("archive", task)}>{t("task.archive")}</button><button className="btn small danger" onClick={() => onLifecycle("trash", task)}>{t("task.trash")}</button></div></details></div></div>
        </div>)}
      </div>}
    </>
  );
}
