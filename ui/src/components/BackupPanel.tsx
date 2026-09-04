import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { BackupInfo } from "../types";
import { fmtDate } from "../format";
import { Note, Spinner } from "./ui";

function fmtSize(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return "–";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

export default function BackupPanel() {
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [kind, setKind] = useState<"db" | "full">("db");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ id: string; createdAt: string | null; plan: Record<string, unknown> } | null>(null);
  const [releaseBusy, setReleaseBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setBackups(await api.listBackups());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      const r = await api.createBackup(kind, note.trim());
      setOk(
        `Sicherung erstellt: ${r.kind}-Snapshot ` +
        `(Integrität: ${r.integrity}${r.data_zip ? ", Audio/Exporte archiviert" : ""}).`,
      );
      setNote("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const previewRestore = async (b: BackupInfo) => {
    setError(null);
    setOk(null);
    setBusy(true);
    try {
      const plan = await api.restoreBackup(b.id, false);
      setPreview({ id: b.id, createdAt: b.created_at, plan });
    } catch (e) {
      setError((e as Error).message);
      setPreview(null);
    } finally {
      setBusy(false);
    }
  };

  const confirmRestore = async () => {
    if (!preview) return;
    if (!window.confirm(
      "Wirklich wiederherstellen? Die aktuelle Datenbank wird durch die Sicherung ersetzt. " +
      "Vorher wird automatisch ein Sicherheits-Snapshot der aktuellen Daten angelegt.",
    )) return;
    setError(null);
    setOk(null);
    setBusy(true);
    try {
      await api.restoreBackup(preview.id, true);
      setOk("Wiederherstellung abgeschlossen. Die Ansicht wird beim nächsten Öffnen aktualisiert.");
      setPreview(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const releaseStorage = async () => {
    if (!window.confirm("Temporäre Dateien löschen? Aufnahmen, Meetings und Sicherungen bleiben erhalten.")) return;
    setReleaseBusy(true); setError(null); setOk(null);
    try {
      const result = await api.releaseStorage();
      setOk(`Temporäre Dateien entfernt (${Number(result.removed_bytes ?? 0).toLocaleString("de-DE")} Bytes).`);
    } catch (e) { setError((e as Error).message); }
    finally { setReleaseBusy(false); }
  };

  if (loading) {
    return <div className="panel"><Spinner label="Lade Sicherungen…" /></div>;
  }

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow">
          <h1 className="detail-title">Sicherung &amp; Wiederherstellung</h1>
          <div className="detail-meta">
            <span>{backups.length} Sicherung(en)</span>
          </div>
        </div>
      </div>

      {error && <Note kind="error">{error}</Note>}
      {ok && <Note kind="ok">{ok}</Note>}

      <div className="card">
        <h2>Neue Sicherung</h2>
        <div className="row wrap filters">
          <label className="field">
            <span>Typ</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as "db" | "full")}>
              <option value="db">Meetings und Einstellungen</option>
              <option value="full">Alles inklusive Aufnahmen und Exporten</option>
            </select>
          </label>
          <label className="field grow">
            <span>Notiz (optional)</span>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="z. B. vor großer Änderung"
              maxLength={512}
            />
          </label>
          <button className="btn primary" onClick={create} disabled={busy}>
            {busy ? "Erstelle…" : "Sicherung erstellen"}
          </button>
        </div>
      </div>

      <details className="card maintenance-details">
        <summary>Wartung</summary>
        <div className="maintenance-content">
          <h2>Temporäre Dateien entfernen</h2>
          <p className="hint">Aufnahmen, Meetings und Sicherungen bleiben erhalten.</p>
          <button className="btn" onClick={() => void releaseStorage()} disabled={releaseBusy}>{releaseBusy ? "Entferne…" : "Temporäre Dateien entfernen"}</button>
        </div>
      </details>

      {!backups.length ? (
        <Note>Keine Sicherungen.</Note>
      ) : (
        <div className="card">
          <h2>Verfügbare Sicherungen</h2>
          <div className="backup-list">
            {backups.map((b) => (
              <div key={b.id} className={"backup-row" + (b.exists ? "" : " missing")}>
                <div className="backup-main">
                  <div className="backup-kind">
                    <span className="badge">{b.kind}</span>
                    {!b.exists && <span className="badge warn">Datei fehlt</span>}
                  </div>
                  <div className="backup-sub">
                    <span>{fmtDate(b.created_at)}</span>
                    <span>{fmtSize(b.size)}</span>
                    {b.note ? <span className="dim">{b.note}</span> : null}
                  </div>
                </div>
                <div className="backup-controls">
                  {b.exists && (
                    <button className="btn small" onClick={() => previewRestore(b)} disabled={busy}>
                      Vorschau
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview && (
        <div className="card restore-preview">
          <h2>Wiederherstellung prüfen</h2>
          <dl className="kv">
            <dt>Sicherung</dt><dd>{preview.createdAt ? fmtDate(preview.createdAt) : "Ausgewählt"}<span className="path-value">{String(preview.plan.path ?? "")}</span></dd>
            <dt>Typ</dt><dd>{String(preview.plan.kind ?? "")}</dd>
            <dt>Prüfung</dt><dd>{String(preview.plan.integrity ?? "")}</dd>
            <dt>Ziel</dt><dd className="path-value">{String(preview.plan.live_db ?? "")}</dd>
          </dl>
          <p className="hint">{String(preview.plan.note ?? "")}</p>
          <div className="row">
            <button className="btn primary" onClick={confirmRestore} disabled={busy}>
              {busy ? "Stelle wieder her…" : "Wirklich wiederherstellen"}
            </button>
            <button className="btn" onClick={() => setPreview(null)} disabled={busy}>Abbrechen</button>
          </div>
        </div>
      )}
    </div>
  );
}
