import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { BackupInfo } from "../types";
import { fmtDate } from "../format";
import { Note, Spinner } from "./ui";
import { useI18n } from "../i18n";
import { activeLocale } from "../i18n/messages";

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
  const { t } = useI18n();
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
      setOk(t("backup.created", { kind: r.kind, integrity: r.integrity }) + (r.data_zip ? t("backup.archived") : "") + ").");
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
    if (!window.confirm(t("backup.confirm_restore"))) return;
    setError(null);
    setOk(null);
    setBusy(true);
    try {
      await api.restoreBackup(preview.id, true);
      setOk(t("backup.restored"));
      setPreview(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const releaseStorage = async () => {
    if (!window.confirm(t("backup.confirm_release"))) return;
    setReleaseBusy(true); setError(null); setOk(null);
    try {
      const result = await api.releaseStorage();
      setOk(t("backup.released", { n: Number(result.removed_bytes ?? 0).toLocaleString(activeLocale()) }));
    } catch (e) { setError((e as Error).message); }
    finally { setReleaseBusy(false); }
  };

  if (loading) {
    return <div className="panel"><Spinner label={t("backup.loading")} /></div>;
  }

  return (
    <div className="panel">
      <div className="head-row">
        <div className="grow">
          <h1 className="detail-title">{t("backup.title")}</h1>
          <div className="detail-meta">
            <span>{t("backup.count", { n: backups.length })}</span>
          </div>
        </div>
      </div>

      {error && <Note kind="error">{error}</Note>}
      {ok && <Note kind="ok">{ok}</Note>}

      <div className="card">
        <h2>{t("backup.new")}</h2>
        <div className="row wrap filters">
          <label className="field">
            <span>{t("common.type")}</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as "db" | "full")}>
              <option value="db">{t("backup.type_db")}</option>
              <option value="full">{t("backup.type_full")}</option>
            </select>
          </label>
          <label className="field grow">
            <span>{t("backup.note_label")}</span>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t("backup.note_placeholder")}
              maxLength={512}
            />
          </label>
          <button className="btn primary" onClick={create} disabled={busy}>
            {busy ? t("backup.creating") : t("backup.create")}
          </button>
        </div>
      </div>

      <details className="card maintenance-details">
        <summary>{t("backup.maintenance")}</summary>
        <div className="maintenance-content">
          <h2>{t("backup.remove_temp")}</h2>
          <p className="hint">{t("backup.release_hint")}</p>
          <button className="btn" onClick={() => void releaseStorage()} disabled={releaseBusy}>{releaseBusy ? t("backup.releasing") : t("backup.remove_temp")}</button>
        </div>
      </details>

      {!backups.length ? (
        <Note>{t("backup.empty")}</Note>
      ) : (
        <div className="card">
          <h2>{t("backup.available")}</h2>
          <div className="backup-list">
            {backups.map((b) => (
              <div key={b.id} className={"backup-row" + (b.exists ? "" : " missing")}>
                <div className="backup-main">
                  <div className="backup-kind">
                    <span className="badge">{b.kind}</span>
                    {!b.exists && <span className="badge warn">{t("backup.missing_file")}</span>}
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
                      {t("common.preview")}
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
          <h2>{t("backup.check_restore")}</h2>
          <dl className="kv">
            <dt>{t("backup.label_backup")}</dt><dd>{preview.createdAt ? fmtDate(preview.createdAt) : t("common.selected")}<span className="path-value">{String(preview.plan.path ?? "")}</span></dd>
            <dt>{t("common.type")}</dt><dd>{String(preview.plan.kind ?? "")}</dd>
            <dt>{t("backup.label_check")}</dt><dd>{String(preview.plan.integrity ?? "")}</dd>
            <dt>{t("backup.label_target")}</dt><dd className="path-value">{String(preview.plan.live_db ?? "")}</dd>
          </dl>
          <p className="hint">{String(preview.plan.note ?? "")}</p>
          <div className="row">
            <button className="btn primary" onClick={confirmRestore} disabled={busy}>
              {busy ? t("backup.restoring") : t("backup.restore_confirm")}
            </button>
            <button className="btn" onClick={() => setPreview(null)} disabled={busy}>{t("common.cancel")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
