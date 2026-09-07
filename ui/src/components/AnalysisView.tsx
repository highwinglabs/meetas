import { useMemo } from "react";
import type { AnalysisData, AnalysisEntry, AnalysisSource, Segment } from "../types";
import { sectionsForLang, missingText, analysisLabel, analysisLangCode } from "../format";
import { Note } from "./ui";
import { useI18n } from "../i18n";

function Entry({ e, refs, isTask, lang }:
  { e: AnalysisEntry; refs: number[]; isTask: boolean; lang: string }) {
  const miss = missingText(lang);
  return (
    <div className="entry">
      <div className="entry-text">
        {e.text}
        {refs.map((n) => (
          <sup key={n} className="chat-cite">
            <button type="button"
              onClick={() => document.getElementById(`analysis-src-${n}`)
                ?.scrollIntoView({ behavior: "smooth", block: "center" })}>
              [{n}]
            </button>
          </sup>
        ))}
      </div>
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
//
// Sources: cited statements carry global [n] markers (1..N over ALL sources in
// document order, identical to the backend markdown export and the chat's
// Quellen list). A "Quellen" section at the bottom lists them; entries that
// resolve to a real segment id open the transcript at that point.
export default function AnalysisView({ content, markdown, model, outputLanguage,
  segments = [], onJumpToSegment }: {
  content: string;
  markdown: string;
  model: string;
  outputLanguage?: string | null;
  /** Loaded transcript segments (legacy S-ids are resolved positionally). */
  segments?: Segment[];
  onJumpToSegment?: (segmentId: string) => void;
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

  // Global [n] numbering over all sources in document order.
  const [refsByEntry, allSources] = useMemo(
    () => {
      const m = new Map<AnalysisEntry, number[]>();
      const sources: AnalysisSource[] = [];
      if (parsed) {
        let n = 0;
        for (const s of sectionsForLang(lang)) {
          for (const e of (parsed[s.key] ?? []) as AnalysisEntry[]) {
            if (!e || typeof e !== "object") continue;
            const srcs = Array.isArray(e.quellen) ? e.quellen : [];
            const refs: number[] = [];
            for (const src of srcs) {
              n += 1;
              refs.push(n);
              sources.push(src);
            }
            if (refs.length > 0) m.set(e, refs);
          }
        }
      }
      return [m, sources];
    },
    [parsed, lang],
  );

  // Resolve a source to a real segment id for the timestamp jump. New
  // analyses carry the real id directly; legacy ones store the positional
  // S-id, which we map to the loaded transcript positionally.
  const resolveSegmentId = (src: AnalysisSource): string | null => {
    if (!src || typeof src !== "object") return null;
    if (src.sid) {
      return /^S\d+$/.test(src.segment_id) ? null : src.segment_id;
    }
    const i = parseInt(src.segment_id.slice(1), 10);
    if (Number.isFinite(i) && segments[i - 1]?.id) return segments[i - 1].id;
    return /^S\d+$/.test(src.segment_id) ? null : src.segment_id;
  };

  if (failed || !parsed) {
    return (
      <div>
        {failed && <Note kind="warn">{t("analysis.format_failed")}</Note>}
        <pre className="markdown-raw">{markdown}</pre>
      </div>
    );
  }

  const miss = missingText(lang);
  // Empty sections are hidden entirely; if nothing at all was extracted, a
  // single placeholder line replaces all nine "not specified" blocks.
  const sections = sectionsForLang(lang).filter((s) => (parsed[s.key] ?? []).length > 0);
  return (
    <div className="analysis">
      <div className="analysis-model dim">{t("analysis.model", { model })}{outputLanguage ? ` · ${t("analysis.language", { lang: outputLanguage })}` : ""}</div>
      {sections.length === 0 ? (
        <div className="na">{miss}</div>
      ) : (
        sections.map((s) => {
          const entries = (parsed[s.key] ?? []) as AnalysisEntry[];
          const isTask = s.key === "aufgaben";
          return (
            <div key={s.key} className="analysis-section">
              <h3>{s.title}</h3>
              <div className="entries">
                {entries.map((e, i) => (
                  <Entry key={i} e={e} refs={refsByEntry.get(e) ?? []} isTask={isTask} lang={lang} />
                ))}
              </div>
            </div>
          );
        })
      )}
      {allSources.length > 0 && (
        <div className="analysis-section">
          <h3>{t("analysis.sources")}</h3>
          <div className="rag-sources">
            {allSources.map((src, i) => {
              const n = i + 1;
              const segId = resolveSegmentId(src);
              const inner = (
                <>
                  <div className="hit-head">
                    <span><strong>{n}.</strong> {[src.sprecher, src.timestamp].filter(Boolean).join(" · ")}</span>
                  </div>
                  {src.snippet && <div className="hit-snippet">{src.snippet}</div>}
                </>
              );
              return segId && onJumpToSegment ? (
                <button key={n} id={`analysis-src-${n}`} className="hit" title={t("analysis.jump")}
                  onClick={() => onJumpToSegment(segId)}>{inner}</button>
              ) : (
                <div key={n} id={`analysis-src-${n}`} className="hit">{inner}</div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
