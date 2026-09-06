import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ModelSpec, SetupCheck, SetupDownloadStatus } from "../types";
import { Note, Spinner } from "./ui";
import { useI18n } from "../i18n";

type Screen = "start" | "asr" | "progress" | "llm" | "done";

function mb(value: number): string {
  return `${Math.max(0, Math.round(value / (1024 * 1024)))} MB`;
}

function speed(value: number): string {
  return `${(value / (1024 * 1024)).toFixed(1)} MB/s`;
}

export default function SetupWizard({ onComplete }: { onComplete: () => void }) {
  const { t } = useI18n();
  const [screen, setScreen] = useState<Screen>("start");
  const [check, setCheck] = useState<SetupCheck | null>(null);
  const [downloading, setDownloading] = useState<SetupDownloadStatus | null>(null);
  const [llms, setLlms] = useState<ModelSpec[]>([]);
  const [llmModel, setLlmModel] = useState("");
  const [confirmAsr, setConfirmAsr] = useState(false);
  const [confirmLlm, setConfirmLlm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const c = await api.setupCheck();
      setCheck(c);
      return c;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
    void api.modelCatalog()
      .then((models) => {
        const list = models.filter((m) => m.kind === "llm");
        setLlms(list);
        setLlmModel((prev) => prev || list[0]?.id || "");
      })
      .catch(() => { /* catalog not reachable yet */ });
  }, [refresh]);

  // Poll the download status while a download runs (1.5 s cadence).
  useEffect(() => {
    if (screen !== "progress" || downloading?.state !== "running") return;
    const id = setInterval(() => {
      void api.setupDownloadStatus()
        .then((s) => {
          setDownloading(s);
          if (s.state !== "running") {
            clearInterval(id);
            if (s.state === "done") {
              setError(null);
              void refresh().then((c) => {
                if (c) setScreen("llm");
              });
            } else {
              setError(s.error ?? t("setup.progress_failed"));
            }
          }
        })
        .catch(() => { /* core briefly unreachable; keep polling */ });
    }, 1500);
    return () => clearInterval(id);
  }, [screen, downloading?.state, refresh, t]);

  // While Ollama is still missing, re-probe every 3 s so the wizard continues
  // automatically as soon as the user finished the install in a terminal.
  useEffect(() => {
    if (screen !== "llm" || !check || check.ollama.reachable || downloading?.state === "running") return;
    const id = setInterval(() => { void refresh(); }, 3000);
    return () => clearInterval(id);
  }, [screen, check?.ollama.reachable, downloading?.state, refresh]);

  // The explicit download click doubles as the confirmation for the
  // network gate: enable network_allowed once so the model download can run
  // without sending the user to the settings screen first.
  const ensureNetwork = async (): Promise<boolean> => {
    if (!check || check.network_allowed) return true;
    await api.updateSettings({ network_allowed: true }, true);
    const c = await refresh();
    return c ? c.network_allowed : false;
  };

  const startAsr = async () => {
    if (!check) return;
    setBusy(true); setError(null);
    try {
      if (!(await ensureNetwork())) {
        setError(t("setup.network_needed"));
        return;
      }
      const s = await api.setupDownload("asr", check.asr.model, confirmAsr);
      setDownloading(s);
      setScreen("progress");
      // Fast downloads (model already cached) can finish before the polling
      // loop below starts; jump ahead instead of waiting for a poll.
      if (s.state === "done") {
        const c = await refresh();
        if (c) setScreen("llm");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const startLlm = async () => {
    setBusy(true); setError(null);
    try {
      if (!(await ensureNetwork())) {
        setError(t("setup.network_needed"));
        return;
      }
      const s = await api.setupDownload("ollama", llmModel, confirmLlm);
      setDownloading(s);
      setScreen("progress");
      // Pulls of models that are already present finish in milliseconds;
      // handle the finished state directly instead of stalling on the
      // progress screen.
      if (s.state === "done") {
        const c = await refresh();
        if (c) setScreen("llm");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true); setError(null);
    try {
      await api.setupComplete();
      setScreen("done");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const deferLlm = async () => {
    setBusy(true); setError(null);
    try {
      await api.setupComplete();
      setScreen("done");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const copyHint = async (hint: string) => {
    try {
      await navigator.clipboard.writeText(hint);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable */ }
  };

  const retry = () => {
    setError(null);
    if (downloading?.kind === "ollama") void startLlm();
    else if (check?.asr.installed) setScreen("llm");
    else { setConfirmAsr(false); setScreen("asr"); }
  };

  if (!check) {
    return <div className="card setup-wizard"><Spinner label={t("setup.loading")} /></div>;
  }

  const asrInstalled = check.asr.installed;
  const llmReady = check.ollama.reachable && check.ollama.has_model;
  const lowDisk = check.disk_free_mb !== null &&
    check.disk_free_mb < check.asr.size_mb * 2;

  return (
    <div className="card setup-wizard">
      <header className="setup-head">
        <div>
          <h1>{t("setup.title")}</h1>
          <p className="dim">{t("setup.subtitle")}</p>
        </div>
        <div className="setup-steps" aria-hidden="true">
          {(["start", "asr", "llm", "done"] as const).map((s, i) => (
            <span key={s} className={`setup-dot ${screen === s || (s === "llm" && screen === "progress") ? "active" : ""}`}>{i + 1}</span>
          ))}
        </div>
      </header>

      {screen === "start" && (
        <section className="setup-screen">
          <h2>{t("setup.start_title")}</h2>
          <ul className="setup-list">
            <li className={check.ffmpeg ? "ok" : "err"}>
              {check.ffmpeg ? t("setup.ffmpeg_ok") : t("setup.ffmpeg_missing")}
              {!check.ffmpeg && (
                <div className="setup-hint">
                  <code>{t("setup.ffmpeg_pacman")}</code>
                  <button className="btn small" onClick={() => void copyHint(t("setup.ffmpeg_pacman"))}>{t("setup.copy")}</button>
                </div>
              )}
            </li>
            <li className={lowDisk ? "warn" : "ok"}>
              {check.disk_free_mb !== null
                ? t("setup.disk_free", { mb: Math.round(check.disk_free_mb / 1024), gb: "" })
                : t("setup.disk_unknown")}
              {lowDisk && <span className="dim">{t("setup.disk_low")}</span>}
            </li>
          </ul>
          {error && <Note kind="error">{error}</Note>}
          <div className="setup-actions">
            <button className="btn primary" onClick={() => setScreen(asrInstalled ? "llm" : "asr")}>{t("setup.next")}</button>
          </div>
        </section>
      )}

      {screen === "asr" && (
        <section className="setup-screen">
          <h2>{t("setup.asr_title")}</h2>
          {asrInstalled ? (
            <>
              <p className="ok-text">{t("setup.asr_ready", { model: check.asr.model })}</p>
              <div className="setup-actions">
                <button className="btn primary" onClick={() => setScreen("llm")}>{t("setup.next")}</button>
              </div>
            </>
          ) : (
            <>
              <p>{t("setup.asr_desc", { model: check.asr.model, size: mb(check.asr.size_mb * 1024 * 1024) })}</p>
              {!check.network_allowed && <p className="dim">{t("setup.network_note")}</p>}
              <label className="setup-confirm">
                <input type="checkbox" checked={confirmAsr} onChange={(e) => setConfirmAsr(e.target.checked)} />
                {t("setup.asr_confirm")}
              </label>
              {error && <Note kind="error">{error}</Note>}
              <div className="setup-actions">
                <button className="btn primary" disabled={!confirmAsr || busy} onClick={() => void startAsr()}>
                  {busy ? t("setup.starting") : t("setup.btn_download")}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {screen === "progress" && (
        <section className="setup-screen">
          <h2>{downloading?.kind === "ollama" ? t("setup.llm_loading_title") : t("setup.progress_title")}</h2>
          {downloading?.state === "running" ? (
            <div className="setup-progress">
              <div className="setup-bar" role="progressbar" aria-label="progress">
                <div
                  className="setup-bar-fill"
                  style={{
                    width: downloading.total_bytes
                      ? `${Math.min(100, (downloading.downloaded_bytes / downloading.total_bytes) * 100).toFixed(1)}%`
                      : undefined,
                  }}
                />
              </div>
              <p className="dim">
                {downloading.total_bytes
                  ? t("setup.progress_detail", { done: mb(downloading.downloaded_bytes), total: mb(downloading.total_bytes) })
                  : mb(downloading.downloaded_bytes)}
                {downloading.speed_bytes_per_s ? ` · ${speed(downloading.speed_bytes_per_s)}` : ""}
                {downloading.file ? ` · ${downloading.file}` : ""}
              </p>
              {downloading.total_bytes === null && <p className="dim">{t("setup.progress_no_total")}</p>}
            </div>
          ) : downloading?.state === "done" ? (
            <p className="ok-text">{t("setup.progress_done")}</p>
          ) : (
            <Note kind="error">{error ?? t("setup.progress_failed")}</Note>
          )}
          {downloading?.state === "error" && (
            <div className="setup-actions">
              <button className="btn" onClick={() => void retry()}>{t("setup.retry")}</button>
            </div>
          )}
        </section>
      )}

      {screen === "llm" && (
        <section className="setup-screen">
          <h2>{t("setup.llm_title")}</h2>
          {llmReady ? (
            <>
              <p className="ok-text">{t("setup.llm_ready", { model: check.ollama.selected_model })}</p>
              {error && <Note kind="error">{error}</Note>}
              <div className="setup-actions">
                <button className="btn primary" disabled={busy} onClick={() => void finish()}>{t("setup.finish")}</button>
              </div>
            </>
          ) : check.ollama.reachable ? (
            <>
              <p>{t("setup.llm_select_desc")}</p>
              {!check.network_allowed && <p className="dim">{t("setup.network_note")}</p>}
              <select className="input" value={llmModel} onChange={(e) => {
                const v = e.target.value;
                setLlmModel(v);
                // Persist the choice immediately: the wizard treats the
                // selected model as the new default summary model, so the
                // ready-state below reflects what the user actually picked.
                void api.updateSettings({ default_summary_model: v })
                  .then(() => void refresh())
                  .catch(() => { /* keep local selection; retry on download */ });
              }}>
                {llms.map((m) => (
                  <option key={m.id} value={m.id}>{m.name} ({m.size})</option>
                ))}
              </select>
              <label className="setup-confirm">
                <input type="checkbox" checked={confirmLlm} onChange={(e) => setConfirmLlm(e.target.checked)} />
                {t("setup.llm_confirm", { model: llmModel })}
              </label>
              {error && <Note kind="error">{error}</Note>}
              <div className="setup-actions">
                <button className="btn primary" disabled={!confirmLlm || busy || !llmModel} onClick={() => void startLlm()}>
                  {busy ? t("setup.starting") : t("setup.llm_install_btn")}
                </button>
                <button className="btn" disabled={busy} onClick={() => void deferLlm()}>{t("setup.later")}</button>
              </div>
            </>
          ) : (
            <>
              <p>{t("setup.llm_missing_desc")}</p>
              {check.ollama.install_hint && (
                <div className="setup-hint">
                  <code>{check.ollama.install_hint}</code>
                  <button className="btn small" onClick={() => void copyHint(check.ollama.install_hint!)}>
                    {copied ? t("setup.copied") : t("setup.copy")}
                  </button>
                </div>
              )}
              <p className="dim">{t("setup.llm_waiting")}</p>
              <div className="setup-actions">
                <button className="btn" disabled={busy} onClick={() => void refresh()}>{t("setup.check")}</button>
                <button className="btn" disabled={busy} onClick={() => void deferLlm()}>{t("setup.later")}</button>
              </div>
            </>
          )}
        </section>
      )}

      {screen === "done" && (
        <section className="setup-screen">
          <h2>{t("setup.done_title")}</h2>
          <p>{t("setup.done_desc")}</p>
          <div className="setup-actions">
            <button className="btn primary" onClick={onComplete}>{t("setup.start_app")}</button>
          </div>
        </section>
      )}
    </div>
  );
}
