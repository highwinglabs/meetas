import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";
import { api } from "../api";
import {
  resolveLang, setActiveLang, translate,
  type LangPref, type MessageKey, type UiLang,
} from "./messages";

export type { LangPref, MessageKey, UiLang } from "./messages";

const STORAGE_KEY = "meetas.uiLang";

function readStored(): LangPref {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "de" || value === "en" || value === "system") return value;
  } catch {
    /* localStorage unavailable (private mode) */
  }
  return "system";
}

type I18nValue = {
  lang: UiLang;
  pref: LangPref;
  setLang: (pref: LangPref) => void;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useState<LangPref>(readStored);
  const lang = resolveLang(pref);

  // The server-persisted preference is the source of truth: it survives
  // browsers that wipe site storage (e.g. LibreWolf). localStorage stays as
  // the instant, offline fallback. "system" on the server means "no forced
  // choice" and never overrides a locally stored explicit selection.
  useEffect(() => {
    let cancelled = false;
    api.settings()
      .then((settings) => {
        if (cancelled) return;
        const value = settings.ui_language;
        if (value === "de" || value === "en") setPref(value);
      })
      .catch(() => { /* core offline: keep stored or system language */ });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang;
    setActiveLang(lang);
  }, [lang]);

  const setLang = useCallback((next: LangPref) => {
    setPref(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
    // Persist server-side (fire-and-forget) so the choice also survives
    // cleared site storage; localStorage already updated the UI instantly.
    void api.updateSettings({ ui_language: next }).catch(() => { /* keep local pref */ });
  }, []);

  const value = useMemo<I18nValue>(() => ({
    lang,
    pref,
    setLang,
    t: (key, vars) => translate(lang, key, vars),
  }), [lang, pref, setLang]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used within I18nProvider");
  return ctx;
}
