import type { AnalysisData } from "./types";

export function fmtHMS(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "--:--:--";
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(ss)}`;
}

export function fmtClock(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "0:00";
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return d.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Berlin" });
}

export function fmtDateOnly(value: string | null | undefined): string {
  if (!value) return "–";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match) {
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    if (!Number.isNaN(date.getTime())) return date.toLocaleDateString("de-DE");
  }
  return fmtDate(value);
}

export const STATUS_LABEL: Record<string, string> = {
  recording: "Aufnahme",
  paused: "Pausiert",
  ready: "Bereit",
  transcribing: "Transkribieren",
  analyzing: "Analyse läuft",
  done: "Fertig",
  failed: "Fehler",
  archived: "Archiviert",
};

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
