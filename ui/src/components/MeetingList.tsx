import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem } from "../types";
import { fmtClock, fmtDate } from "../format";
import { Badge, Note } from "./ui";

export default function MeetingList({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<MeetingListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await api.listMeetings());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const trash = async (id: string) => {
    if (!window.confirm("Meeting in den Papierkorb verschieben? Es wird nicht endgültig gelöscht.")) return;
    try { await api.trashMeeting(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };

  useEffect(() => { load(); }, [load]);

  if (error) {
    return <div className="panel"><h1>Meetings</h1><Note kind="error">{error}</Note></div>;
  }
  if (!items) {
    return <div className="panel"><h1>Meetings</h1><Note>Wird geladen…</Note></div>;
  }

  const filteredItems = items.filter((meeting) =>
    meeting.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow"><h1>Meetings</h1></div>
        <button className="btn" onClick={load}>Aktualisieren</button>
      </div>
      {items.length > 0 && <div className="meeting-list-tools"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Meetings durchsuchen…" aria-label="Meetings durchsuchen" /><span className="dim">{filteredItems.length} von {items.length}</span></div>}
      {items.length === 0 ? (
        <Note>Keine Meetings.</Note>
      ) : filteredItems.length === 0 ? (
        <Note>Keine passenden Meetings.</Note>
      ) : (
        <div className="meeting-list">
          {filteredItems.map((m) => (
            <div key={m.id} className="meeting-item">
              <button className="meeting-main meeting-open" onClick={() => onOpen(m.id)}>
                <div className="meeting-title">{m.title}</div>
                <div className="meeting-sub">
                  {fmtDate(m.start_at)} · {fmtClock(m.duration_s)} · {m.segments} Segmente
                </div>
              </button>
              {m.status !== "done" && <Badge status={m.status} />}
              <button className="btn small danger" onClick={() => void trash(m.id)} disabled={m.status === "recording" || m.status === "paused"} title={m.status === "recording" || m.status === "paused" ? "Aktive Aufnahmen können nicht gelöscht werden." : undefined}>Papierkorb</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
