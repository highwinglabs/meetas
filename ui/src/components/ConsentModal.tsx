import { useState } from "react";
import { api } from "../api";
import { Note } from "./ui";
import { useI18n } from "../i18n";

export default function ConsentModal({ text, open, onClose, onAcknowledged }: {
  text: string;
  open: boolean;
  onClose: () => void;
  onAcknowledged: () => void;
}) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const ack = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.ackConsent(true);
      onAcknowledged();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>{t("consent.title")}</h2>
        <p className="consent-text">{text}</p>
        {error && <Note kind="error">{error}</Note>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose} disabled={busy}>{t("consent.later")}</button>
          <button className="btn primary" onClick={ack} disabled={busy}>
            {busy ? t("consent.saving") : t("consent.acknowledge")}
          </button>
        </div>
      </div>
    </div>
  );
}
