import type { AnalysisData, AnalysisEntry } from "../types";
import { sectionsForLang, missingText, analysisLabel, analysisLangCode } from "../format";
import { Note } from "./ui";
import { useI18n } from "../i18n";

function Entry({ e, isTask, lang }: { e: AnalysisEntry; isTask: boolean; lang: string }) {
  const miss = missingText(lang);
  return (
    <div className="entry">
      <div className="entry-text">{e.text}</div>
      {isTask && (
        <div className="task-meta">
          <span className="chip">{analysisLabel("verantwortlich", lang)}: {e.verantwortlich ?? miss}</span>
          <span className="chip">{analysisLabel("frist", lang)}: {e.deadline ?? miss}</span>
        </div>
      )}
    </div>
  );
}

// Renders the structured 9-area analysis. The `content` field is the validated
// JSON produced by the LLM (see core/analysis/schema.py). We fall back to the
// rendered markdown if parsing fails. Section headings, the "not specified"
// placeholder and the action-item labels follow the stored analysis output
// language (`outputLanguage`); legacy analyses fall back to German.
export default function AnalysisView({ content, markdown, model, outputLanguage }: {
  content: string;
  markdown: string;
  model: string;
  outputLanguage?: string | null;
}) {
  const lang = analysisLangCode(outputLanguage);
  const { t } = useI18n();
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
        {failed && <Note kind="warn">{t("analysis.format_failed")}</Note>}
        <pre className="markdown-raw">{markdown}</pre>
      </div>
    );
  }

  const miss = missingText(lang);
  const sections = sectionsForLang(lang);
  return (
    <div className="analysis">
      <div className="analysis-model dim">{t("analysis.model", { model })}{outputLanguage ? ` · ${t("analysis.language", { lang: outputLanguage })}` : ""}</div>
      {sections.map((s) => {
        const entries = (parsed[s.key] ?? []) as AnalysisEntry[];
        const isTask = s.key === "aufgaben";
        return (
          <div key={s.key} className="analysis-section">
            <h3>{s.title}</h3>
            {entries.length === 0 ? (
              <div className="na">{miss}</div>
            ) : (
              <div className="entries">
                {entries.map((e, i) => (
                  <Entry key={i} e={e} isTask={isTask} lang={lang} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
