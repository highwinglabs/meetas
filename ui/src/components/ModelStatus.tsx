import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AppSettings, BenchmarkResult, MeetingListItem, ModelSpec } from "../types";
import { Note, Spinner } from "./ui";

export default function ModelStatus() {
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [meetings, setMeetings] = useState<MeetingListItem[]>([]);
  const [testMeeting, setTestMeeting] = useState("");
  const [benchmark, setBenchmark] = useState<BenchmarkResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [llmTest, setLlmTest] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setModels(await api.modelCatalog()); } catch (e) { setError((e as Error).message); }
    try { setSettings(await api.settings()); } catch { /* offline */ }
    try { setMeetings(await api.listMeetings()); } catch { /* offline */ }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const installAsr = async (model: ModelSpec) => {
    setBusy(model.id); setError(null); setOk(null);
    try { const r = await api.downloadAsr(true, model.id); setOk(`${r.name} ist jetzt ${r.ready ? "installiert und bereit" : "nicht installiert"}.`); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const installLlm = async (model: ModelSpec) => {
    if (!window.confirm(`„${model.name}“ aus Ollama herunterladen? Dafür wird ein Netzwerkzugriff benötigt.`)) return;
    setBusy(`install:${model.id}`); setError(null); setOk(null);
    try { const result = await api.installLlm(model.id, true); setOk(`${result.name} ist jetzt installiert.`); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const choose = async (key: "live_asr_model" | "quality_asr_model" | "default_summary_model", model: string) => {
    setBusy(model); setError(null);
    try { setSettings(await api.updateSettings({ [key]: model })); setOk("Vorauswahl gespeichert."); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const runBenchmark = async (model: string) => {
    setBusy(`benchmark:${model}`); setError(null); setBenchmark(null);
    try {
      const result = await api.benchmarkAsr(model, testMeeting || null);
      setBenchmark(result as unknown as BenchmarkResult);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const testLlm = async (model: string) => {
    setBusy(`llm:${model}`); setError(null); setLlmTest(null);
    try {
      const result = await api.testLlm(model);
      const name = models.find((item) => item.id === model)?.name ?? "KI-Modell";
      setLlmTest(`${name}: Antwort „${result.response}“ in ${result.seconds} s`);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const asr = models.filter((m) => m.kind === "asr");
  const llms = models.filter((m) => m.kind === "llm");
  const activeAsrIds = settings ? new Set([settings.live_asr_model, settings.quality_asr_model]) : new Set<string>();
  const activeLlmIds = settings ? new Set([settings.default_summary_model, settings.quality_analysis_model]) : new Set<string>();
  const activeLlms = llms.filter((m) => activeLlmIds.has(m.id));
  const unavailableActiveLlms = activeLlms.filter((m) => !m.installed);
  const installedLlms = llms.filter((m) => m.installed && !activeLlmIds.has(m.id));
  const otherLlms = llms.filter((m) => !m.installed && !activeLlmIds.has(m.id));
  const activeAsr = asr.filter((m) => m.installed && activeAsrIds.has(m.id));
  const installedAsr = asr.filter((m) => m.installed && !activeAsrIds.has(m.id));
  const otherAsr = asr.filter((m) => !m.installed && !activeAsrIds.has(m.id));
  const unavailableActiveAsr = asr.filter((m) => !m.installed && activeAsrIds.has(m.id));
  const asrUsage = (model: ModelSpec) => [
    model.id === settings?.live_asr_model ? "Live-Text" : null,
    model.id === settings?.quality_asr_model ? "vollständige Transkripte" : null,
  ].filter(Boolean).join(" und ");
  const llmUsage = (model: ModelSpec) => [
    model.id === settings?.default_summary_model ? "schnelle Auswertungen" : null,
    model.id === settings?.quality_analysis_model ? "Auswertungen mit höchster Qualität" : null,
  ].filter(Boolean).join(" und ");
  const renderGroupHeader = (label: string, count: number, tone: string) => <div className={`model-group-head ${tone}`}><h3>{label}</h3><span className="count">{count}</span></div>;
  return <div className="panel">
    <h1>Modelle</h1>{error && <Note kind="error">{error}</Note>}{ok && <Note kind="ok">{ok}</Note>}
    <details className="model-section" open><summary>Transkription</summary>
    <div className="card"><h2>Transkription</h2>{!settings ? <Spinner label="Lade…" /> : <div className="model-groups">
      {(activeAsr.length > 0 || unavailableActiveAsr.length > 0) && <section className="model-group selected">{renderGroupHeader("Ausgewählt", activeAsr.length + unavailableActiveAsr.length, "selected")}{activeAsr.map((m) => <div className="model-row" key={`active-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">Für {asrUsage(m)}</span></div></div>)}{unavailableActiveAsr.map((m) => <div className="model-row" key={`active-unavailable-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">Für {asrUsage(m)}</span><span className="model-missing">Nicht lokal installiert</span></div></div>)}</section>}
      {installedAsr.length > 0 && <section className="model-group installed">{renderGroupHeader("Installiert", installedAsr.length, "installed")}{installedAsr.map((m) => <div className="model-row" key={`installed-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">{m.purpose} · {m.size}</span></div><div className="model-controls"><button className="btn small" onClick={() => void choose("live_asr_model", m.id)}>Für Live-Text</button><button className="btn small" onClick={() => void choose("quality_asr_model", m.id)}>Für vollständiges Transkript</button><button className="btn small" onClick={() => void runBenchmark(m.id)} disabled={busy !== null}>{busy === `benchmark:${m.id}` ? "Teste…" : "Testen"}</button></div></div>)}</section>}
      {otherAsr.length > 0 && <section className="model-group missing">{renderGroupHeader("Nicht installiert", otherAsr.length, "missing")}{otherAsr.map((m) => <div className="model-row" key={`other-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">{m.purpose} · {m.size}</span><span className="model-missing">Nicht installiert</span></div><div className="model-controls"><button className="btn small" onClick={() => void installAsr(m)} disabled={busy !== null}>{busy === m.id ? "Installiere…" : "Installieren"}</button></div></div>)}</section>}
    </div>}
      <details className="benchmark-details">
        <summary>Transkriptionsmodell testen</summary>
        <div className="benchmark-controls"><label className="field"><span>Testaufnahme</span><select value={testMeeting} onChange={(e) => setTestMeeting(e.target.value)}><option value="">Neueste lokale Aufnahme</option>{meetings.filter((m) => m.original_path).map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}</select></label></div>
        {benchmark && <div className="benchmark-result"><strong>{models.find((item) => item.id === benchmark.model)?.name ?? "Testergebnis"}</strong><span>{benchmark.audio_seconds ?? "?"} s Audio · {benchmark.processing_seconds} s Verarbeitung · Faktor {benchmark.realtime_factor ?? "?"}</span><span>{benchmark.live_possible ? "Für Live-Text schnell genug" : "Für Live-Text zu langsam"}</span><pre>{benchmark.segments.map((s) => s.text).join(" ") || "(kein Text erkannt)"}</pre></div>}
      </details>
    </div></details>
    <details className="model-section" open><summary>KI für Analyse und Chat</summary>
    <div className="card"><h2>KI für Analyse und Chat</h2>{!settings ? <Spinner label="Lade…" /> : <div className="model-groups">
      {(activeLlms.filter((m) => m.installed).length > 0 || unavailableActiveLlms.length > 0) && <section className="model-group selected">{renderGroupHeader("Ausgewählt", activeLlms.filter((m) => m.installed).length + unavailableActiveLlms.length, "selected")}{activeLlms.filter((m) => m.installed).map((m) => <div className="model-row" key={`active-llm-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">{m.purpose}</span><span className="model-ready">Für {llmUsage(m)}</span></div><button className="btn small" onClick={() => void testLlm(m.id)} disabled={busy !== null}>{busy === `llm:${m.id}` ? "Teste…" : "Testen"}</button></div>)}{unavailableActiveLlms.map((m) => <div className="model-row" key={`active-llm-unavailable-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">Für {llmUsage(m)}</span><span className="model-missing">Nicht lokal installiert</span></div></div>)}</section>}
      {installedLlms.length > 0 && <section className="model-group installed">{renderGroupHeader("Installiert", installedLlms.length, "installed")}{installedLlms.map((m) => <div className="model-row" key={`installed-llm-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">{m.purpose}</span></div><button className="btn small" onClick={() => void choose("default_summary_model", m.id)} disabled={busy !== null}>Für neue Meetings vorauswählen</button><button className="btn small" onClick={() => void testLlm(m.id)} disabled={busy !== null}>{busy === `llm:${m.id}` ? "Teste…" : "Testen"}</button></div>)}</section>}
      {otherLlms.length > 0 && <section className="model-group missing">{renderGroupHeader("Nicht installiert", otherLlms.length, "missing")}{otherLlms.map((m) => <div className="model-row" key={`other-llm-${m.id}`}><div className="model-main"><strong>{m.name}</strong><span className="dim">{m.purpose}</span><span className="model-missing">Noch nicht installiert</span></div><div className="model-controls"><button className="btn small" onClick={() => void installLlm(m)} disabled={busy !== null}>{busy === `install:${m.id}` ? "Installiere…" : "Installieren"}</button></div></div>)}</section>}
      {llmTest && <Note kind="ok">{llmTest}</Note>}
    </div>}</div></details>
  </div>;
}
