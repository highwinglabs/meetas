import type { Marker } from "../types";
import { fmtHMS } from "../format";
import { Note } from "./ui";
import { useI18n } from "../i18n";

export default function MarkersTab({
  markers,
  onDelete,
}: {
  markers: Marker[];
  onDelete: (markerId: string) => void;
}) {
  const { t } = useI18n();
  return (
    <section className="block">
      <h2>{t("markers.title")} <span className="count">{markers.length}</span></h2>
      {!markers.length ? (
        <Note>{t("markers.empty")}</Note>
      ) : (
        <div className="marker-list">
          {markers.map((m) => (
            <div key={m.id} className="marker-row">
              <span className="marker-time">{fmtHMS(m.at_s)}</span>
              <span className="marker-text">{m.text}</span>
              <span className="grow" />
              <button className="btn small danger" onClick={() => onDelete(m.id)}>{t("common.delete")}</button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
