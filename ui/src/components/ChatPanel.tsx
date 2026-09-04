import type { RagAnswer } from "../types";

export type MeetingChatMessage = { question: string; answer: RagAnswer };

export default function ChatPanel({
  question,
  onQuestionChange,
  busy,
  canAsk,
  onAsk,
  messages,
  onClearMessages,
  onDeleteMessage,
  onJumpToSegment,
}: {
  question: string;
  onQuestionChange: (value: string) => void;
  busy: boolean;
  canAsk: boolean;
  onAsk: () => void;
  messages: MeetingChatMessage[];
  onClearMessages: () => void;
  onDeleteMessage: (index: number) => void;
  onJumpToSegment: (segmentId: string) => void;
}) {
  return (
    <section className="block ai-questions">
      <h2>Frage zu diesem Meeting</h2>
      <form className="search-form" onSubmit={(e) => { e.preventDefault(); onAsk(); }}>
        <input value={question} onChange={(e) => onQuestionChange(e.target.value)}
          placeholder="z. B. Was wurde entschieden? Welche Aufgaben sind offen?" aria-label="Frage zu diesem Meeting" />
        <button className="btn primary" type="submit" disabled={busy || !question.trim() || !canAsk}>
          {busy ? "Denke…" : "Fragen"}
        </button>
      </form>
      {messages.length > 0 && <div className="chat-history">
        <div className="row spread chat-history-head"><strong>Gesprächsverlauf</strong><button className="btn small" type="button" onClick={onClearMessages}>Verlauf löschen</button></div>
        {messages.map((message, index) => (
          <details className="chat-turn chat-turn-collapsible" key={`${index}-${message.question}`}>
            <summary className="chat-question"><strong>Du</strong><span>{message.question}</span></summary>
            <div className="chat-turn-actions"><button className="btn small danger" type="button" onClick={() => onDeleteMessage(index)}>Chat löschen</button></div>
            <div className="rag">
              <div className={"note " + (message.answer.grounded ? "ok" : "warn")}>
                {message.answer.grounded ? "Antwort aus diesem Transkript." : "Keine ausreichende Information im Transkript gefunden."}
              </div>
              <p className="rag-answer">{message.answer.answer}</p>
              {message.answer.sources.length > 0 && <div className="rag-sources">
                <h3>Quellen</h3>
                {message.answer.sources.map((s) => (
                  <button key={`${index}-${s.segment_id}`} className="hit" onClick={() => onJumpToSegment(s.segment_id)}>
                    <div className="hit-head"><span>{s.speaker_id ?? "Sprecher"}</span><span className="hit-time">{s.timestamp}</span></div>
                    <div className="hit-snippet">{s.snippet}</div>
                  </button>
                ))}
              </div>}
            </div>
          </details>
        ))}
      </div>}
    </section>
  );
}
