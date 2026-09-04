import type { Marker } from "../types";
import { fmtHMS } from "../format";
import { Note } from "./ui";

export default function MarkersTab({
  markers,
  onDelete,
}: {
  markers: Marker[];
  onDelete: (markerId: string) => void;
}) {
  return (
    <section className="block">
      <h2>Marker <span className="count">{markers.length}</span></h2>
      {!markers.length ? (
        <Note>Keine Marker. Markiere Stellen direkt im Transkript mit ⚑.</Note>
      ) : (
        <div className="marker-list">
          {markers.map((m) => (
            <div key={m.id} className="marker-row">
              <span className="marker-time">{fmtHMS(m.at_s)}</span>
              <span className="marker-text">{m.text}</span>
              <span className="grow" />
              <button className="btn small danger" onClick={() => onDelete(m.id)}>Löschen</button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
