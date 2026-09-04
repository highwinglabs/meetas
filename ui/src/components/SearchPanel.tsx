import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { MeetingListItem, ModelSpec, Project, RagAnswer, SearchHit } from "../types";
import { fmtDateOnly, fmtHMS } from "../format";
import { Note, Spinner } from "./ui";

type Mode = "fts" | "rag";

export default function SearchPanel({
  onOpenMeeting,
}: {
  onOpenMeeting: (id: string, segmentId?: string) => void;
}) {
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
      setError("Für Fragen muss ein installiertes KI-Modell ausgewählt sein.");
      return;
    }
    if (scopeType === "selected" && selectedMeetingIds.length === 0) {
      setError("Bitte mindestens ein Meeting auswählen.");
      return;
    }
    if (scopeType === "meeting" && !scope) {
      setError("Bitte ein Meeting auswählen.");
      return;
    }
    if (scopeType === "project" && !projectId) {
      setError("Bitte ein Projekt auswählen.");
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
      <h1>Suche</h1>
      <form className="search-form" onSubmit={run}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={mode === "rag"
              ? "Frage stellen (z. B. Was wurde zur Deadline entschieden?)"
              : "Suchbegriff (z. B. Budget, Release)"}
        />
        <button className="btn primary" type="submit" disabled={busy || (isQuestionMode && !chatModel)}>
          {busy ? "Läuft…" : isQuestionMode ? "Beantworten" : "Suchen"}
        </button>
      </form>

      <div className="search-modes" aria-label="Was möchtest du tun?">
        <button type="button" className={textSearchMode ? "chip active" : "chip"} onClick={() => setMode("fts")}>Text</button>
        <button type="button" className={mode === "rag" ? "chip active" : "chip"} onClick={() => setMode("rag")}>KI fragen</button>
      </div>

      <details className="search-scope-card">
        <summary>Eingrenzen</summary>
        <div className="search-scopes">
          {([['all', 'Alle Meetings'], ['project', 'Projekt auswählen'], ['meeting', 'Meeting auswählen'], ['selected', 'Mehrere Meetings']] as const).map(([value, label]) => (
            <button type="button" key={value} className={scopeType === value ? "chip active" : "chip"} onClick={() => setScopeType(value)}>{label}</button>
          ))}
        </div>
        <div className="seg-filter">
          {scopeType === "meeting" && <label>Meeting <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">Meeting wählen</option>
            {meetings.map((m) => <option key={m.id} value={m.id}>{m.title || m.id.slice(0, 8)}</option>)}
          </select></label>}
          {scopeType === "project" && <label>Projekt <select value={projectId} onChange={(e) => setProjectId(e.target.value)}><option value="">Projekt wählen</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
          {scopeType === "selected" && <div className="meeting-picker">
            <input value={meetingQuery} onChange={(e) => setMeetingQuery(e.target.value)} placeholder="Meetings filtern…" aria-label="Meetings filtern" />
            <div className="meeting-picker-count">{selectedMeetingIds.length ? `${selectedMeetingIds.length} ausgewählt` : "Noch kein Meeting ausgewählt"}</div>
            <div className="meeting-picker-list">
              {filteredMeetings.map((m) => <label key={m.id} className="check"><input type="checkbox" checked={selectedMeetingIds.includes(m.id)} onChange={(e) => setSelectedMeetingIds((current) => e.target.checked ? [...current, m.id] : current.filter((id) => id !== m.id))} /><span className="check-title">{m.title || m.id.slice(0, 8)}</span></label>)}
              {!filteredMeetings.length && <span className="dim">Keine passenden Meetings.</span>}
            </div>
          </div>}
        </div>
      </details>

      {isQuestionMode && <div className="search-model-select">
        <label>KI-Modell <select value={chatModel} onChange={(e) => setChatModel(e.target.value)} disabled={busy}>
          {installedChatModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select></label>
        {!installedChatModels.length && <span className="hint">Kein installiertes KI-Modell verfügbar.</span>}
      </div>}

      {error && <Note kind="error">{error}</Note>}
      {busy && <Spinner label={isQuestionMode ? "Erstelle Antwort…" : "Suche läuft…"} />}

      {answer && mode === "rag" && !busy && (
        <div className="rag">
          <div className={"note " + (answer.grounded ? "ok" : "warn")}>
            {answer.grounded
              ? "Antwort aus den ausgewählten Inhalten."
              : "Keine ausreichende Information im Transkript gefunden."}
          </div>
          <p className="rag-answer">{answer.answer}</p>
          {answer.sources.length > 0 && (
            <div className="rag-sources">
              <h3>Quellen</h3>
              {answer.sources.map((s) => (
                s.source_kind === "document" ? (
                  <div className="hit" key={s.segment_id}>
                    <div className="hit-head"><span className="hit-title">{s.file_name ?? "Projektdatei"}</span>{s.locator && <span className="dim">{s.locator}</span>}</div>
                    <div className="hit-snippet">{s.snippet}</div>
                    {s.file_id && <a className="link" href={api.projectFileUrl(s.file_id)} download={s.file_name ?? undefined}>Datei öffnen →</a>}
                  </div>
                ) : (
                  <button key={s.segment_id} className="hit" disabled={!s.meeting_id} onClick={() => s.meeting_id && onOpenMeeting(s.meeting_id, s.segment_id)}>
                    <div className="hit-head"><span className="hit-title">{s.meeting_title ?? s.meeting_id?.slice(0, 8)}</span>{s.meeting_date && <span className="dim">{fmtDateOnly(s.meeting_date)}</span>}{s.speaker_id && <span className="dim">{s.speaker_id}</span>}<span className="hit-time">{s.timestamp}</span></div>
                    <div className="hit-snippet">{s.snippet}</div>
                    {s.meeting_id && <div className="dim">Zur Stelle springen →</div>}
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
            <Note>Keine Treffer im gewählten Bereich.</Note>
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
