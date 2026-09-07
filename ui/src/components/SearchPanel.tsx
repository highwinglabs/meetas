import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { MeetingListItem, ModelSpec, Project, RagAnswer, SearchHit } from "../types";
import { fmtDateOnly, fmtHMS } from "../format";
import { Note, Spinner } from "./ui";
import { useI18n } from "../i18n";

type Mode = "fts" | "rag";

export default function SearchPanel({
  onOpenMeeting,
}: {
  onOpenMeeting: (id: string, segmentId?: string) => void;
}) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("fts");
  const [scope, setScope] = useState<string>("");
  const [meetings, setMeetings] = useState<MeetingListItem[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [answer, setAnswer] = useState<RagAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scopeType, setScopeType] = useState<"all" | "meeting" | "project" | "selected">("all");
  const [projectId, setProjectId] = useState("");
  const [selectedMeetingIds, setSelectedMeetingIds] = useState<string[]>([]);
  const [meetingQuery, setMeetingQuery] = useState("");
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [chatModel, setChatModel] = useState("");

  useEffect(() => {
    api.listMeetings().then(setMeetings).catch(() => setMeetings([]));
    api.projects().then(setProjects).catch(() => setProjects([]));
    api.modelCatalog().then(setModels).catch(() => setModels([]));
  }, []);

  const installedChatModels = models.filter((model) => model.kind === "llm" && model.installed);
  useEffect(() => {
    if (!installedChatModels.length) {
      setChatModel("");
      return;
    }
    setChatModel((current) => installedChatModels.some((model) => model.id === current)
      ? current : installedChatModels[0].id);
  }, [models]);

  const run = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!q.trim()) return;
    if (mode === "rag" && !chatModel) {
      setError(t("search.needs_model"));
      return;
    }
    if (scopeType === "selected" && selectedMeetingIds.length === 0) {
      setError(t("search.pick_meetings"));
      return;
    }
    if (scopeType === "meeting" && !scope) {
      setError(t("search.pick_meeting"));
      return;
    }
    if (scopeType === "project" && !projectId) {
      setError(t("search.pick_project"));
      return;
    }
    setBusy(true);
    setError(null);
    setHits(null);
    setAnswer(null);
    const meetingId = scopeType === "meeting" ? (scope || null) : null;
    const selectedProject = scopeType === "project" ? (projectId || null) : null;
    try {
      if (mode === "rag") {
        setAnswer(await api.chat(q.trim(), { limit: 25, meeting_id: meetingId, project_id: selectedProject, meeting_ids: scopeType === "selected" ? selectedMeetingIds : [], model: chatModel }));
      } else {
        const r = await api.search(q.trim(), 25, meetingId, selectedProject, scopeType === "selected" ? selectedMeetingIds : []);
        setHits(r.results);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const showResults = hits !== null && mode === "fts" && !busy;
  const isQuestionMode = mode === "rag";
  const textSearchMode = mode === "fts";
  const filteredMeetings = meetings.filter((meeting) =>
    (meeting.title || meeting.id).toLocaleLowerCase().includes(meetingQuery.trim().toLocaleLowerCase()),
  );

  return (
    <div className="panel">
      <h1>{t("search.title")}</h1>
      <form className="search-form" onSubmit={run}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={mode === "rag"
              ? t("search.placeholder_question")
              : t("search.placeholder_text")}
        />
        <button className="btn primary" type="submit" disabled={busy || (isQuestionMode && !chatModel)}>
          {busy ? t("search.running") : isQuestionMode ? t("search.answer") : t("search.search")}
        </button>
      </form>

      <div className="search-modes" aria-label={t("search.mode_aria")}>
        <button type="button" className={textSearchMode ? "chip active" : "chip"} onClick={() => setMode("fts")}>{t("search.mode_text")}</button>
        <button type="button" className={mode === "rag" ? "chip active" : "chip"} onClick={() => setMode("rag")}>{t("search.mode_ai")}</button>
      </div>

      <details className="search-scope-card">
        <summary>{t("search.narrow")}</summary>
        <div className="search-scopes">
          {([['all', "search.scope_all"], ['project', "search.scope_project"], ['meeting', "search.scope_meeting"], ['selected', "search.scope_selected"]] as const).map(([value, msgKey]) => (
            <button type="button" key={value} className={scopeType === value ? "chip active" : "chip"} onClick={() => setScopeType(value)}>{t(msgKey)}</button>
          ))}
        </div>
        <div className="seg-filter">
          {scopeType === "meeting" && <label>{t("common.meeting")} <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">{t("search.select_meeting")}</option>
            {meetings.map((m) => <option key={m.id} value={m.id}>{m.title || m.id.slice(0, 8)}</option>)}
          </select></label>}
          {scopeType === "project" && <label>{t("common.project")} <select value={projectId} onChange={(e) => setProjectId(e.target.value)}><option value="">{t("search.select_project")}</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
          {scopeType === "selected" && <div className="meeting-picker">
            <input value={meetingQuery} onChange={(e) => setMeetingQuery(e.target.value)} placeholder={t("search.filter_meetings")} aria-label={t("search.filter_meetings_aria")} />
            <div className="meeting-picker-count">{selectedMeetingIds.length ? t("search.selected_count", { n: selectedMeetingIds.length }) : t("search.none_selected")}</div>
            <div className="meeting-picker-list">
              {filteredMeetings.map((m) => <label key={m.id} className="check"><input type="checkbox" checked={selectedMeetingIds.includes(m.id)} onChange={(e) => setSelectedMeetingIds((current) => e.target.checked ? [...current, m.id] : current.filter((id) => id !== m.id))} /><span className="check-title">{m.title || m.id.slice(0, 8)}</span></label>)}
              {!filteredMeetings.length && <span className="dim">{t("search.no_matching")}</span>}
            </div>
          </div>}
        </div>
      </details>

      {isQuestionMode && <div className="search-model-select">
        <label>{t("common.ai_model")} <select value={chatModel} onChange={(e) => setChatModel(e.target.value)} disabled={busy}>
          {installedChatModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select></label>
        {!installedChatModels.length && <span className="hint">{t("project.no_model")}</span>}
      </div>}

      {error && <Note kind="error">{error}</Note>}
      {busy && <Spinner label={isQuestionMode ? t("search.creating_answer") : t("search.searching")} />}

      {answer && mode === "rag" && !busy && (
        <div className="rag">
          <div className={"note " + (answer.grounded ? "ok" : "warn")}>
            {answer.grounded
              ? t("search.grounded")
              : answer.evidence === "partial"
                ? t("search.partial")
                : t("chat.no_info")}
          </div>
          <p className="rag-answer">{answer.answer}</p>
          {answer.sources.length > 0 && (
            <div className="rag-sources">
              <span className="dim">{t("chat.sources")}</span>
              {answer.sources.map((s) => (
                s.source_kind === "document" ? (
                  <div className="hit" key={s.segment_id}>
                    <div className="hit-head"><span className="hit-title">{s.file_name ?? t("search.project_file")}</span>{s.locator && <span className="dim">{s.locator}</span>}</div>
                    <div className="hit-snippet">{s.snippet}</div>
                    {s.file_id && <a className="link" href={api.projectFileUrl(s.file_id)} download={s.file_name ?? undefined}>{t("search.open_file")}</a>}
                  </div>
                ) : (
                  <button key={s.segment_id} className="hit" disabled={!s.meeting_id} onClick={() => s.meeting_id && onOpenMeeting(s.meeting_id, s.segment_id)}>
                    <div className="hit-head"><span className="hit-title">{s.meeting_title ?? s.meeting_id?.slice(0, 8)}</span>{s.meeting_date && <span className="dim">{fmtDateOnly(s.meeting_date)}</span>}{s.speaker_id && <span className="dim">{s.speaker_id}</span>}<span className="hit-time">{s.timestamp}</span></div>
                    <div className="hit-snippet">{s.snippet}</div>
                    {s.meeting_id && <div className="dim">{t("search.jump_to")}</div>}
                  </button>
                )
              ))}
            </div>
          )}
        </div>
      )}

      {showResults && (
        <div className="search-results">
          {(hits as SearchHit[]).length === 0 ? (
            <Note>{t("search.no_results")}</Note>
          ) : (
            (hits as SearchHit[]).map((h) => (
              <button
                key={h.segment_id}
                className="hit"
                onClick={() => onOpenMeeting(h.meeting_id, h.segment_id)}
              >
                <div className="hit-head">
                  <span className="hit-title">{h.meeting_title ?? h.meeting_id.slice(0, 8)}</span>
                  {h.meeting_date && <span className="dim">{fmtDateOnly(h.meeting_date)}</span>}
                  <span className="hit-time">{fmtHMS(h.start_s)}</span>
                  {h.speaker_id ? <span className="dim">{h.speaker_id}</span> : null}
                </div>
                <div className="hit-snippet">{h.snippet}</div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
