import type { AnalysisData, AnalysisEntry } from "../types";
import { SECTIONS } from "../format";
import { Note } from "./ui";

function Entry({ e, isTask }: { e: AnalysisEntry; isTask: boolean }) {
  return (
    <div className="entry">
      <div className="entry-text">{e.text}</div>
      {isTask && (
        <div className="task-meta">
          <span className="chip">Verantwortlich: {e.verantwortlich ?? "nicht angegeben"}</span>
          <span className="chip">Frist: {e.deadline ?? "nicht angegeben"}</span>
        </div>
      )}
    </div>
  );
}

// Renders the structured 9-area analysis. The `content` field is the validated
// JSON produced by the LLM (see core/analysis/schema.py). We fall back to the
// rendered markdown if parsing fails.
export default function AnalysisView({ content, markdown, model }: {
  content: string;
  markdown: string;
  model: string;
}) {
  let parsed: AnalysisData | null = null;
  let failed: string | null = null;
  try {
    parsed = JSON.parse(content) as AnalysisData;
  } catch (e) {
    failed = (e as Error).message;
  }

  if (failed || !parsed) {
    return (
      <div>
        {failed && <Note kind="warn">Die Auswertung konnte nicht vollständig formatiert werden. Der Inhalt wird vereinfacht angezeigt.</Note>}
        <pre className="markdown-raw">{markdown}</pre>
      </div>
    );
  }

  return (
    <div className="analysis">
      <div className="analysis-model dim">Modell: {model}</div>
      {SECTIONS.map((s) => {
        const entries = (parsed[s.key] ?? []) as AnalysisEntry[];
        const isTask = s.key === "aufgaben";
        return (
          <div key={s.key} className="analysis-section">
            <h3>{s.title}</h3>
            {entries.length === 0 ? (
              <div className="na">nicht angegeben</div>
            ) : (
              <div className="entries">
                {entries.map((e, i) => (
                  <Entry key={i} e={e} isTask={isTask} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
