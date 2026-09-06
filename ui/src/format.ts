import type { AnalysisData } from "./types";
import { activeLocale, type MessageKey } from "./i18n/messages";

export function fmtHMS(sec: number | null | undefined): string | null {
  if (sec == null || Number.isNaN(sec)) return null;
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(ss)}`;
}

export function fmtClock(sec: number | null | undefined): string | null {
  if (sec == null || Number.isNaN(sec)) return null;
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

// 4950 -> "1 h 23 min"; null/NaN -> null
export function fmtDuration(sec: number | null | undefined): string | null {
  if (sec == null || Number.isNaN(sec)) return null;
  const total = Math.max(0, Math.round(sec));
  if (total < 60) return `${total} s`;
  const m = Math.floor(total / 60);
  const secPart = total % 60;
  if (m < 60) return secPart > 0 ? `${m} min ${secPart} s` : `${m} min`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm > 0 ? `${h} h ${mm} min` : `${h} h`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return d.toLocaleString(activeLocale(), { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDateOnly(value: string | null | undefined): string {
  if (!value) return "–";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match) {
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    if (!Number.isNaN(date.getTime())) return date.toLocaleDateString(activeLocale());
  }
  return fmtDate(value);
}

const STATUS_KEYS: Record<string, MessageKey> = {
  recording: "status.recording",
  paused: "status.paused",
  ready: "status.ready",
  transcribing: "status.transcribing",
  analyzing: "status.analyzing",
  done: "status.done",
  failed: "status.failed",
  archived: "status.archived",
};

export function statusKey(status: string): MessageKey | null {
  return STATUS_KEYS[status] ?? null;
}

export function statusClass(status: string): string {
  if (status === "recording" || status === "paused") return "live";
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  if (status === "transcribing" || status === "analyzing") return "working";
  return "idle";
}

// 0..100 meter from a dBFS value (practical range ~ -60..0 dB).
export function levelToPct(levelDb: number | null | undefined): number {
  if (levelDb == null || Number.isNaN(levelDb)) return 0;
  const v = ((levelDb + 60) / 60) * 100;
  return Math.max(0, Math.min(100, v));
}

// The 9 analysis areas, in display order (mirrors core/analysis/schema.py).
export const SECTIONS: { key: keyof AnalysisData; title: string }[] = [
  { key: "kurzfassung", title: "Kurzfassung" },
  { key: "themen", title: "Themen" },
  { key: "entscheidungen", title: "Entscheidungen" },
  { key: "aufgaben", title: "Aufgaben / Action Items" },
  { key: "offene_fragen", title: "Offene Fragen" },
  { key: "naechste_schritte", title: "Nächste Schritte" },
  { key: "risiken", title: "Risiken / Probleme" },
  { key: "wichtige_fakten", title: "Wichtige Fakten" },
  { key: "follow_ups", title: "Follow-ups" },
];

// Localised analysis display strings (de/en), mirroring core/analysis/schema.py.
// Only the two output languages reachable via the per-meeting analysis-language
// control are provided; anything else (incl. a legacy null) falls back to German.
const SECTIONS_EN: { key: keyof AnalysisData; title: string }[] = [
  { key: "kurzfassung", title: "Summary" },
  { key: "themen", title: "Topics" },
  { key: "entscheidungen", title: "Decisions" },
  { key: "aufgaben", title: "Action Items" },
  { key: "offene_fragen", title: "Open Questions" },
  { key: "naechste_schritte", title: "Next Steps" },
  { key: "risiken", title: "Risks / Issues" },
  { key: "wichtige_fakten", title: "Key Facts" },
  { key: "follow_ups", title: "Follow-ups" },
];
const SECTIONS_BY_LANG: Record<string, { key: keyof AnalysisData; title: string }[]> = {
  de: SECTIONS,
  en: SECTIONS_EN,
};
const MISSING_BY_LANG: Record<string, string> = { de: "nicht angegeben", en: "not specified" };
const ANALYSIS_LABELS_BY_LANG: Record<string, Record<string, string>> = {
  de: { verantwortlich: "Verantwortlich", frist: "Frist" },
  en: { verantwortlich: "Owner", frist: "Deadline" },
};

export function analysisLangCode(lang: string | null | undefined): string {
  const code = (lang ?? "").trim().toLowerCase().split("-")[0];
  return code === "en" ? "en" : "de";
}

export function sectionsForLang(lang: string | null | undefined): { key: keyof AnalysisData; title: string }[] {
  return SECTIONS_BY_LANG[analysisLangCode(lang)] ?? SECTIONS;
}

export function missingText(lang: string | null | undefined): string {
  return MISSING_BY_LANG[analysisLangCode(lang)] ?? "nicht angegeben";
}

export function analysisLabel(name: string, lang: string | null | undefined): string {
  const table = ANALYSIS_LABELS_BY_LANG[analysisLangCode(lang)] ?? ANALYSIS_LABELS_BY_LANG.de;
  return table[name] ?? ANALYSIS_LABELS_BY_LANG.de[name] ?? name;
}
