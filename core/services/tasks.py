"""Task lifecycle, history and overview. (part of MeetingService)."""
from __future__ import annotations

from sqlalchemy import delete, func, or_, select
from core.analysis.schema import NOT_GIVEN
from core.tasks import STATUSES as TASK_STATUSES, extract_tasks
from core.store.db import session_scope
from core.store.models import Meeting, Task, TaskHistory, Project, utcnow
from core.logging_setup import get_logger
from core.services._common import utc_iso, _task_display_status, UnknownMeetingError, UnknownTaskError

log = get_logger("ma.service")


class TasksMixin:

    # --- Phase 6: central task overview ---
    def list_tasks(self, status: str | None = None, owner: str | None = None,
                   limit: int = 200, meeting_id: str | None = None,
                   project_id: str | None = None, view: str = "active") -> list[dict]:
        if view not in {"active", "archived", "trash", "all"}:
            raise ValueError("Unbekannte Aufgabenansicht")
        limit = max(1, min(int(limit), 1000))
        with session_scope() as s:
            q = (select(Task)
                 .order_by(Task.status, Task.sort_order, Task.created_at.desc(), Task.id)
                 )
            if view == "active":
                q = q.where(Task.archived_at.is_(None), Task.deleted_at.is_(None))
            elif view == "archived":
                q = q.where(Task.archived_at.is_not(None), Task.deleted_at.is_(None))
            elif view == "trash":
                q = q.where(Task.deleted_at.is_not(None))
            # ``ueberfaellig`` and ``ohne_deadline`` are derived display
            # states, never values stored in Task.status. Applying them in SQL
            # would therefore always return zero rows.
            if status in TASK_STATUSES:
                q = q.where(Task.status == status)
            if owner:
                q = q.where(func.lower(Task.responsible) == owner.lower())
            if meeting_id:
                q = q.where(Task.meeting_id == meeting_id)
            if project_id:
                # Older manually-created tasks may only carry their meeting
                # reference. Treat them as project tasks as well, so moving a
                # meeting into a project does not hide its existing tasks.
                project_meetings = select(Meeting.id).where(Meeting.project_id == project_id)
                q = q.where(or_(Task.project_id == project_id,
                                Task.meeting_id.in_(project_meetings)))
            tasks = s.scalars(q).all()
            if status in {"ueberfaellig", "ohne_deadline"}:
                tasks = [t for t in tasks if _task_display_status(t) == status]
            tasks = tasks[:limit]
            mids = {t.meeting_id for t in tasks if t.meeting_id}
            pids = {t.project_id for t in tasks if t.project_id}
            titles = {}
            project_names = {}
            if mids:
                trows = s.execute(
                    select(Meeting.id, Meeting.title).where(Meeting.id.in_(mids))
                ).all()
                titles = {r.id: r.title for r in trows}
            if pids:
                prows = s.execute(select(Project.id, Project.name).where(Project.id.in_(pids))).all()
                project_names = {r.id: r.name for r in prows}
            return [
                {
                    "id": t.id, "meeting_id": t.meeting_id,
                    "meeting_title": titles.get(t.meeting_id),
                    "project_id": t.project_id,
                    "project_name": project_names.get(t.project_id),
                    "text": t.text, "owner": t.responsible,
                    "deadline": t.due_date, "status": t.status,
                    "display_status": _task_display_status(t),
                    "tags": t.tags,
                    "source_segment_id": t.source_segment_id,
                    "source": "KI extrahiert" if t.source_analysis_id else "manuell",
                    "created_at": utc_iso(t.created_at),
                    "archived_at": utc_iso(t.archived_at),
                    "deleted_at": utc_iso(t.deleted_at),
                }
                for t in tasks
            ]

    def create_manual_task(self, text: str, meeting_id: str | None = None,
                           project_id: str | None = None,
                           responsible: str | None = None,
                           due_date: str | None = None) -> dict:
        if not isinstance(text, str):
            raise ValueError("Der Aufgabentext muss Text sein.")
        if responsible is not None and not isinstance(responsible, str):
            raise ValueError("Verantwortlich muss Text sein.")
        if due_date is not None and not isinstance(due_date, str):
            raise ValueError("Die Deadline muss Text sein.")
        text = text.strip()
        if not text:
            raise ValueError("Eine Aufgabe darf nicht leer sein.")
        if len(text) > 20_000:
            raise ValueError("Die Aufgabe ist zu lang.")
        with session_scope() as s:
            meeting = None
            if meeting_id:
                meeting = s.get(Meeting, meeting_id)
                if meeting is None or meeting.deleted_at is not None:
                    raise KeyError(meeting_id)
                if project_id is None:
                    project_id = meeting.project_id
            if project_id:
                project = s.get(Project, project_id)
                if project is None or project.deleted_at is not None:
                    raise KeyError(project_id)
                if meeting is not None and meeting.project_id not in (None, project_id):
                    raise ValueError("Meeting und Projekt der Aufgabe gehören nicht zusammen.")
            task = Task(meeting_id=meeting_id, project_id=project_id, text=text,
                        responsible=(responsible or "").strip() or NOT_GIVEN,
                        due_date=(due_date or "").strip() or NOT_GIVEN,
                        status="offen", source_segment_id=None,
                        source_analysis_id=None)
            s.add(task)
            s.flush()
            s.add(TaskHistory(task_id=task.id, meeting_id=task.meeting_id,
                              field="created", old_value="", new_value="manuell"))
            s.commit()
            return {"id": task.id, "meeting_id": task.meeting_id,
                    "project_id": task.project_id, "text": task.text,
                    "owner": task.responsible, "deadline": task.due_date,
                    "status": task.status, "source": "manuell",
                    "archived_at": None, "deleted_at": None}

    def update_task(self, task_id: str, status: str | None = None,
                    owner: str | None = None, text: str | None = None,
                    deadline: str | None = None) -> dict:
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                raise UnknownTaskError(task_id)
            if t.deleted_at is not None:
                raise ValueError("Eine Aufgabe im Papierkorb muss zuerst wiederhergestellt werden.")
            if status is not None:
                if status not in TASK_STATUSES:
                    raise ValueError(f"Unbekannter Status '{status}' "
                                     f"(gültig: {', '.join(TASK_STATUSES)})")
                if t.status != status:
                    s.add(TaskHistory(task_id=t.id, meeting_id=t.meeting_id,
                                      field="status", old_value=t.status,
                                      new_value=status))
                t.status = status
            if owner is not None:
                new_owner = owner.strip() or NOT_GIVEN
                if t.responsible != new_owner:
                    s.add(TaskHistory(task_id=t.id, meeting_id=t.meeting_id,
                                      field="responsible", old_value=t.responsible,
                                      new_value=new_owner))
                t.responsible = new_owner
            if text is not None:
                new_text = text.strip()
                if not new_text:
                    raise ValueError("Eine Aufgabe darf nicht leer sein")
                if t.text != new_text:
                    s.add(TaskHistory(task_id=t.id, meeting_id=t.meeting_id,
                                      field="text", old_value=t.text,
                                      new_value=new_text))
                    t.text = new_text
            if deadline is not None:
                new_deadline = deadline.strip() or NOT_GIVEN
                if t.due_date != new_deadline:
                    s.add(TaskHistory(task_id=t.id, meeting_id=t.meeting_id,
                                      field="deadline", old_value=t.due_date,
                                      new_value=new_deadline))
                    t.due_date = new_deadline
            s.commit()
            return {"id": t.id, "text": t.text, "owner": t.responsible,
                    "deadline": t.due_date, "status": t.status, "tags": t.tags}

    def archive_task(self, task_id: str) -> dict:
        """Move a task to the archive without losing it or its history."""
        with session_scope() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise UnknownTaskError(task_id)
            if task.deleted_at is not None:
                raise ValueError("Eine Aufgabe im Papierkorb kann nicht archiviert werden.")
            if task.archived_at is None:
                task.archived_at = utcnow()
                s.add(TaskHistory(task_id=task.id, meeting_id=task.meeting_id,
                                  field="archived", old_value="", new_value="true"))
            s.commit()
            return {"id": task.id, "archived": True,
                    "archived_at": utc_iso(task.archived_at)}

    def restore_task(self, task_id: str) -> dict:
        """Restore an archived or trashed task to the active task list."""
        with session_scope() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise UnknownTaskError(task_id)
            was_archived = task.archived_at is not None
            was_deleted = task.deleted_at is not None
            if not was_archived and not was_deleted:
                return {"id": task.id, "restored": True}
            if was_archived:
                s.add(TaskHistory(task_id=task.id, meeting_id=task.meeting_id,
                                  field="archived", old_value="true", new_value=""))
            if was_deleted:
                s.add(TaskHistory(task_id=task.id, meeting_id=task.meeting_id,
                                  field="deleted", old_value="true", new_value=""))
            task.archived_at = None
            task.deleted_at = None
            s.commit()
            return {"id": task.id, "restored": True}

    def trash_task(self, task_id: str) -> dict:
        """Move a task to the paper bin; this is deliberately reversible."""
        with session_scope() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise UnknownTaskError(task_id)
            if task.deleted_at is None:
                task.deleted_at = utcnow()
                task.archived_at = None
                s.add(TaskHistory(task_id=task.id, meeting_id=task.meeting_id,
                                  field="deleted", old_value="", new_value="true"))
            s.commit()
            return {"id": task.id, "deleted_at": utc_iso(task.deleted_at)}

    def permanently_delete_task(self, task_id: str, confirm: bool = False) -> dict:
        """Irreversibly remove a task, but only after explicit confirmation."""
        if not confirm:
            raise ValueError("Endgültiges Löschen muss ausdrücklich bestätigt werden.")
        with session_scope() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise UnknownTaskError(task_id)
            if task.deleted_at is None:
                raise ValueError("Eine Aufgabe muss zuerst in den Papierkorb verschoben werden.")
            # Be explicit rather than relying on SQLite's FK cascade setting.
            s.query(TaskHistory).filter(TaskHistory.task_id == task_id).delete(
                synchronize_session=False)
            s.delete(task)
            s.commit()
        return {"id": task_id, "deleted": True}

    def task_history(self, task_id: str) -> list[dict]:
        with session_scope() as s:
            if s.get(Task, task_id) is None:
                raise UnknownTaskError(task_id)
            rows = s.scalars(select(TaskHistory).where(
                TaskHistory.task_id == task_id).order_by(TaskHistory.created_at)).all()
            return [{"id": r.id, "task_id": r.task_id, "meeting_id": r.meeting_id,
                     "field": r.field, "old_value": r.old_value,
                     "new_value": r.new_value,
                     "created_at": utc_iso(r.created_at)}
                    for r in rows]

    def task_overview(self, view: str = "active") -> dict:
        if view not in {"active", "archived", "trash", "all"}:
            raise ValueError("Unbekannte Aufgabenansicht")
        with session_scope() as s:
            q = select(Task)
            if view == "active":
                q = q.where(Task.archived_at.is_(None), Task.deleted_at.is_(None))
            elif view == "archived":
                q = q.where(Task.archived_at.is_not(None), Task.deleted_at.is_(None))
            elif view == "trash":
                q = q.where(Task.deleted_at.is_not(None))
            tasks = s.scalars(q).all()
            by_status = {st: 0 for st in (*TASK_STATUSES, "ueberfaellig", "ohne_deadline")}
            for task in tasks:
                display_status = _task_display_status(task)
                if display_status not in by_status:
                    # Keep the overview usable even if an old/corrupt row has
                    # an unknown status; the integrity report will flag it.
                    by_status[display_status] = 0
                by_status[display_status] += 1
            total = sum(by_status.values())
            return {
                "total": total,
                "by_status": by_status,
                "open": by_status["offen"] + by_status["laeuft"] + by_status["ueberfaellig"] + by_status["ohne_deadline"],
            }

    def extract_tasks_for_meeting(self, meeting_id: str) -> dict:
        """Re-run task extraction for a meeting (idempotent)."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            res = extract_tasks(s, meeting_id)
            s.commit()
        return res
