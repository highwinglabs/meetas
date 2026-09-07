import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Consent } from "./types";
import ConsentModal from "./components/ConsentModal";
import RecordingPanel from "./components/RecordingPanel";
import MeetingList from "./components/MeetingList";
import MeetingDetail from "./components/MeetingDetail";
import SearchPanel from "./components/SearchPanel";
import ModelStatus from "./components/ModelStatus";
import TasksPanel from "./components/TasksPanel";
import SetupWizard from "./components/SetupWizard";
import BackupPanel from "./components/BackupPanel";
import ProjectsPanel from "./components/ProjectsPanel";
import SettingsPanel from "./components/SettingsPanel";
import TrashPanel from "./components/TrashPanel";
import { Note } from "./components/ui";
import { useI18n } from "./i18n";

type Tab = "recording" | "meetings" | "projects" | "search" | "tasks" | "models" | "settings" | "backup" | "trash";

const NAV_ICON_PATHS: Record<string, string[]> = {
  recording: ["M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z", "M5 11a7 7 0 0 0 14 0", "M12 18v3"],
  meetings: ["M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6Z", "M4 10h16", "M9 3v4", "M15 3v4"],
  projects: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"],
  tasks: ["M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5Z", "m8.5 12.5 2.5 2.5 5-5.5"],
  search: ["M11 4.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13Z", "m20 20-4.35-4.35"],
  settings: ["M4 7h8", "M16 7h4", "M14 4.5v5", "M4 17h4", "M12 17h8", "M10 14.5v5"],
  general: ["M5 7h14", "M5 12h14", "M5 17h9"],
  models: ["M7 7h10v10H7Z", "M10 2v3", "M14 2v3", "M10 19v3", "M14 19v3", "M2 10h3", "M2 14h3", "M19 10h3", "M19 14h3"],
  backup: ["M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3Z", "M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6", "M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"],
  trash: ["M4 7h16", "M10 11v6M14 11v6", "M6 7l1 13h10l1-13M9 7V4h6v3"],
};

function NavIcon({ name }: { name: string }) {
  return (
    <span className="nav-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        {NAV_ICON_PATHS[name].map((d) => <path key={d} d={d} />)}
      </svg>
    </span>
  );
}

export default function App() {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("recording");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedSeg, setSelectedSeg] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  // First-run wizard: shown until setup is complete. Derived from the core, not
  // stored in the UI; if /setup/check is unreachable we fall back to the main
  // app (fail-safe) so a missing endpoint can never trap the user.
  const [setupNeeded, setSetupNeeded] = useState(false);
  const [consent, setConsent] = useState<Consent | null>(null);
  const [consentOpen, setConsentOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const refreshHealth = useCallback(async () => {
    try {
      await api.health();
      setConnected(true);
    } catch {
      setConnected(false);
    }
  }, []);

  const refreshConsent = useCallback(async () => {
    try {
      const c = await api.consent();
      setConsent(c);
      if (!c.acknowledged) setConsentOpen(true);
    } catch {
      /* offline */
    }
  }, []);

  const refreshSetup = useCallback(async () => {
    try {
      const c = await api.setupCheck();
      // Respect a deliberate completion or "Später" skip; the ASR model
      // is re-checked at point of use (transcribe → 409 with a clear hint).
      setSetupNeeded(!c.setup_completed);
    } catch {
      setSetupNeeded(false);
    }
  }, []);

  useEffect(() => {
    refreshHealth();
    refreshConsent();
    refreshSetup();
    const t = setInterval(refreshHealth, 5000);
    return () => clearInterval(t);
  }, [refreshHealth, refreshConsent, refreshSetup]);

  const consentOk = consent?.acknowledged ?? false;
  const adminTab = tab === "settings" || tab === "models" || tab === "backup";
  const openMeeting = (id: string, segmentId?: string) => {
    setSelectedId(id);
    setSelectedSeg(segmentId ?? null);
    setTab("meetings");
  };
  const goMeetingsList = () => {
    setSelectedId(null);
    setSelectedSeg(null);
    setTab("meetings");
  };

  return (
    <div className={`app-shell${sidebarCollapsed ? " collapsed" : ""}`}>
      <aside className="sidebar">
        <button className="sidebar-toggle" onClick={() => setSidebarCollapsed((value) => !value)} aria-label={t("nav.toggle")}>{sidebarCollapsed ? "☰" : "‹"}</button>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">M</span>
          <span className="brand-name">{t("app.brand")}</span>
        </div>
        <nav className="side-nav" aria-label={t("nav.primary")}>
          <button className={tab === "recording" ? "tab active" : "tab"} onClick={() => { setSelectedId(null); setSelectedSeg(null); setTab("recording"); }}><NavIcon name="recording" /><span>{t("nav.recording")}</span></button>
          <button className={tab === "meetings" ? "tab active" : "tab"} onClick={goMeetingsList}><NavIcon name="meetings" /><span>{t("nav.meetings")}</span></button>
          <button className={tab === "projects" ? "tab active" : "tab"} onClick={() => setTab("projects")}><NavIcon name="projects" /><span>{t("nav.projects")}</span></button>
          <button className={tab === "tasks" ? "tab active" : "tab"} onClick={() => setTab("tasks")}><NavIcon name="tasks" /><span>{t("nav.tasks")}</span></button>
          <button className={tab === "search" ? "tab active" : "tab"} onClick={() => setTab("search")}><NavIcon name="search" /><span>{t("nav.search")}</span></button>
        </nav>
        <details className="nav-admin" open={adminTab}>
          <summary className={adminTab ? "tab active" : "tab"}><NavIcon name="settings" /><span>{t("nav.settings")}</span></summary>
          <div className="admin-nav">
            <button className={tab === "settings" ? "tab active" : "tab"} onClick={() => setTab("settings")}><NavIcon name="general" /><span>{t("nav.general")}</span></button>
            <button className={tab === "models" ? "tab active" : "tab"} onClick={() => setTab("models")}><NavIcon name="models" /><span>{t("nav.models")}</span></button>
            <button className={tab === "backup" ? "tab active" : "tab"} onClick={() => setTab("backup")}><NavIcon name="backup" /><span>{t("nav.backup")}</span></button>
          </div>
        </details>
        <nav className="side-nav secondary trash-nav" aria-label={t("nav.secondary")}>
          <button className={tab === "trash" ? "tab active" : "tab"} onClick={() => setTab("trash")}><NavIcon name="trash" /><span>{t("nav.trash")}</span></button>
        </nav>
      </aside>
      <div className="app">
      {connected === false && (
        <Note kind="warn">
          {t("app.offline")}
        </Note>
      )}

      <main className="content">
        {setupNeeded ? (
          <SetupWizard onComplete={() => setSetupNeeded(false)} />
        ) : (
          <>
            {tab === "recording" && (
              <RecordingPanel consentOk={consentOk} onNeedConsent={() => setConsentOpen(true)} />
            )}
            {tab === "meetings" && (
              selectedId ? (
                <MeetingDetail id={selectedId} segmentId={selectedSeg} onBack={() => { setSelectedId(null); setSelectedSeg(null); }} />
              ) : (
                <MeetingList onOpen={openMeeting} />
              )
            )}
            {tab === "projects" && <ProjectsPanel onOpenMeeting={openMeeting} />}
            {tab === "search" && <SearchPanel onOpenMeeting={openMeeting} />}
            {tab === "tasks" && <TasksPanel onOpenMeeting={openMeeting} />}
            {tab === "models" && <ModelStatus />}
            {tab === "settings" && <SettingsPanel />}
            {tab === "backup" && <BackupPanel />}
            {tab === "trash" && <TrashPanel />}
          </>
        )}
      </main>

      <ConsentModal
        text={consent?.text ?? ""}
        open={consentOpen}
        onClose={() => setConsentOpen(false)}
        onAcknowledged={() => setConsent((c) => (c ? { ...c, acknowledged: true } : c))}
      />
      </div>
    </div>
  );
}
