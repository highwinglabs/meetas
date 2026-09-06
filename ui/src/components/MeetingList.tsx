import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingListItem } from "../types";
import { fmtDate, fmtDuration } from "../format";
import { Badge, Note } from "./ui";
import { useI18n } from "../i18n";

export default function MeetingList({ onOpen }: { onOpen: (id: string) => void }) {
  const { t } = useI18n();
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
    if (!window.confirm(t("meetings.trash_confirm"))) return;
    try { await api.trashMeeting(id); await load(); }
    catch (e) { setError((e as Error).message); }
  };

  useEffect(() => { load(); }, [load]);

  if (error) {
    return <div className="panel"><h1>{t("meetings.title")}</h1><Note kind="error">{error}</Note></div>;
  }
  if (!items) {
    return <div className="panel"><h1>{t("meetings.title")}</h1><Note>{t("meetings.loading")}</Note></div>;
  }

  const filteredItems = items.filter((meeting) =>
    meeting.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow"><h1>{t("meetings.title")}</h1></div>
        <button className="btn" onClick={load}>{t("common.refresh")}</button>
      </div>
      {items.length > 0 && <div className="meeting-list-tools"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("meetings.search_placeholder")} aria-label={t("meetings.search_aria")} />{query && <span className="dim">{t("meetings.count", { shown: filteredItems.length, total: items.length })}</span>}</div>}
      {items.length === 0 ? (
        <Note>{t("meetings.empty")}</Note>
      ) : filteredItems.length === 0 ? (
        <Note>{t("meetings.no_match")}</Note>
      ) : (
        <div className="meeting-list">
          {filteredItems.map((m) => (
            <div key={m.id} className="meeting-item">
              <button className="meeting-main meeting-open" onClick={() => onOpen(m.id)}>
                <div className="meeting-title">{m.title}</div>
                <div className="meeting-sub">
                  {[
                    fmtDate(m.start_at),
                    m.duration_s != null && m.duration_s > 0 ? fmtDuration(m.duration_s) : null,
                    m.segments > 0 ? t("meetings.segments", { n: m.segments }) : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              </button>
              {m.status !== "done" && <Badge status={m.status} />}
              <button className="btn small danger" onClick={() => void trash(m.id)} disabled={m.status === "recording" || m.status === "paused"} title={m.status === "recording" || m.status === "paused" ? t("meetings.trash_disabled") : undefined}>{t("meetings.trash_button")}</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
