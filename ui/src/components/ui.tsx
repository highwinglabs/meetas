import type { ReactNode } from "react";
import { STATUS_LABEL, statusClass } from "../format";

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
  return <span className={`badge ${statusClass(status)}`}>{STATUS_LABEL[status] ?? status}</span>;
}
