import type { ReactNode } from "react";
import { statusClass, statusKey } from "../format";
import { useI18n } from "../i18n";

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="spinner-wrap">
      <span className="spinner" aria-hidden />
      {label ? <span className="spinner-label">{label}</span> : null}
    </div>
  );
}

export function Note({ kind = "info", children }: { kind?: "info" | "warn" | "error" | "ok"; children: ReactNode }) {
  return <div className={`note ${kind}`}>{children}</div>;
}

export function Badge({ status }: { status: string }) {
  const { t } = useI18n();
  const key = statusKey(status);
  return <span className={`badge ${statusClass(status)}`}>{key ? t(key) : status}</span>;
}
