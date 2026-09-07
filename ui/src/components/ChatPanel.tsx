import type { RagAnswer, RagSource } from "../types";
import { useI18n } from "../i18n";

export type MeetingChatMessage = { question: string; answer: RagAnswer };

/** Renders the answer text and turns `[n]` markers into clickable references
 *  that scroll to the matching numbered source below. */
function AnswerWithRefs({ text, turnIndex }: { text: string; turnIndex: number }) {
  const parts = text.split(/(\[\d+\])/g);
  return (<>{parts.map((part, i) => {
    const m = part.match(/^\[(\d+)\]$/);
    if (!m) return <span key={i}>{part}</span>;
    const target = `chat-src-${turnIndex}-${m[1]}`;
    return (
      <sup key={i} className="chat-cite">
        <button type="button"
          onClick={() => document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "center" })}>
          [{m[1]}]
        </button>
      </sup>
    );
  })}</>);
}

function SourceList({ sources, turnIndex, onJumpToSegment }:
  { sources: RagSource[]; turnIndex: number; onJumpToSegment: (segmentId: string) => void }) {
  const { t } = useI18n();
  return (
    <div className="rag-sources">
      <span className="dim">{t("chat.sources")}</span>
      {sources.map((s, n) => (
        <button key={`${turnIndex}-${s.segment_id}`} id={`chat-src-${turnIndex}-${n + 1}`} className="hit"
          onClick={() => onJumpToSegment(s.segment_id)}>
          <div className="hit-head"><span><strong>{n + 1}.</strong> {s.speaker_id ?? t("common.speaker")}</span><span className="hit-time">{s.timestamp}</span></div>
          <div className="hit-snippet">{s.snippet}</div>
        </button>
      ))}
    </div>
  );
}

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
  const { t } = useI18n();
  return (
    <section className="block ai-questions">
      <h2>{t("chat.title")}</h2>
      <form className="search-form" onSubmit={(e) => { e.preventDefault(); onAsk(); }}>
        <input value={question} onChange={(e) => onQuestionChange(e.target.value)}
          placeholder={t("chat.placeholder")} aria-label={t("chat.title")} />
        <button className="btn primary" type="submit" disabled={busy || !question.trim() || !canAsk}>
          {busy ? t("chat.thinking") : t("chat.ask")}
        </button>
      </form>
      {messages.length > 0 && <div className="chat-history">
        <div className="row spread chat-history-head"><strong>{t("chat.history")}</strong><button className="btn small" type="button" onClick={onClearMessages}>{t("chat.clear_history")}</button></div>
        {messages.map((message, index) => (
          <details className="chat-turn chat-turn-collapsible" key={`${index}-${message.question}`}>
            <summary className="chat-question"><strong>{t("chat.you")}</strong><span>{message.question}</span></summary>
            <div className="chat-turn-actions"><button className="btn small danger" type="button" onClick={() => onDeleteMessage(index)}>{t("chat.delete")}</button></div>
            <div className="rag">
              <div className={"note " + (message.answer.grounded ? "ok" : "warn")}>
                {message.answer.grounded
                  ? t("chat.grounded")
                  : message.answer.evidence === "partial"
                    ? t("chat.partial")
                    : t("chat.no_info")}
              </div>
              <p className="rag-answer"><AnswerWithRefs text={message.answer.answer} turnIndex={index} /></p>
              {message.answer.sources.length > 0 && <SourceList sources={message.answer.sources} turnIndex={index} onJumpToSegment={onJumpToSegment} />}
            </div>
          </details>
        ))}
      </div>}
    </section>
  );
}
