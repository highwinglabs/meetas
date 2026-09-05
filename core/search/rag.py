"""Grounded RAG over the meeting corpus (Phase 5).

Retrieves the top-``k`` segments for a question (hybrid search), builds a compact
numbered context, and asks the local LLM to answer **only** from that context,
citing the source segment ids.

Grounding contract (Phase 8 rework):
- An answer is ``grounded`` **only** when it cites at least one segment id that
  actually exists in the retrieved hits. A fluent answer with no valid citation
  is treated as *not answered* and replaced with an explicit "no sufficient
  evidence" message -- ``grounded=True`` is never granted merely because the
  model produced some text.
- Returned ``sources`` are retrieved hits (cited segments first; remaining
  context is included for transparency). Each carries meeting, date, speaker,
  timestamp and a jump target so the UI can navigate straight to the line.
"""
from __future__ import annotations

import re

from core.analysis.schema import NOT_GIVEN
from core.llm.base import LLMEngine, LLMError, LLMResult
from core.llm.output import visible_answer

_SYSTEM = (
    "Du bist ein praeziser Assistent fuer Meeting-Notizen. Beantworte NUR auf "
    "Basis des bereitgestellten Kontexts. Zitiere jede belastbare Aussage mit "
    "dem genauen Marker [seg:<id>], wobei <id> nur eines der oben im Kontext "
    "gelisteten seg-Ids sein darf -- keine anderen Ids erfinden. Wenn du die "
    "Frage beantwortest, muss mindestens ein solches Zitat enthalten sein. "
    "Wenn der Kontext die Frage nicht (ausreichend) beantwortet, antworte "
    "EXAKT: 'Nicht im Transkript beantwortet.' Erfinde keine Fakten, Namen, "
    "Zahlen oder Zeitpunkte. Antworte auf Deutsch, knappe, sachte formulierte "
    "Absaetze."
)

_NOT_ANSWERED = "Nicht im Transkript beantwortet."
_NO_EVIDENCE = "Keine ausreichende Information im Transkript gefunden."


def _fmt_ts(s: float | None) -> str:
    if s is None:
        return "--:--"
    s = max(0, int(round(s)))
    return f"{s // 60:02d}:{s % 60:02d}"


def build_context(hits: list[dict], max_chars: int = 12000) -> str:
    """Render retrieved hits as numbered, source-tagged context blocks."""
    blocks: list[str] = []
    total = 0
    for i, h in enumerate(hits, start=1):
        speaker = h.get("speaker_id") or NOT_GIVEN
        if h.get("source_kind") == "document":
            source = f"Datei: {h.get('file_name') or h.get('meeting_title') or '?'}"
            if h.get("locator"):
                source += f" | {h['locator']}"
            line = f"[{i}] (seg:{h['segment_id']} | {source})\n{h.get('text') or ''}"
        else:
            line = (
                f"[{i}] (seg:{h['segment_id']} | {h.get('meeting_title') or '?'} | "
                f"{speaker} | {_fmt_ts(h.get('start_s'))})\n"
                f"{h.get('text') or ''}"
            )
        if total + len(line) > max_chars and blocks:
            break
        blocks.append(line)
        total += len(line)
    return "\n\n".join(blocks)


def _extract_citations(answer: str) -> list[str]:
    """All seg-ids the model cited, in order of first appearance (deduped)."""
    seen: dict[str, None] = {}
    for m in re.finditer(r"seg:([A-Za-z0-9]+)", answer or ""):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _jump_target(meeting_id: str | None, segment_id: str | None) -> str | None:
    """A relative anchor the UI can use to scroll to the transcript line."""
    if not meeting_id or not segment_id:
        return None
    return f"/meetings/{meeting_id}#seg-{segment_id}"


def _source_from_hit(h: dict) -> dict:
    """Build a source object with the required traceability fields. `h` must be
    a retrieved hit (i.e. a real transcript segment)."""
    return {
        "segment_id": h["segment_id"],
        "meeting_id": h.get("meeting_id"),
        "meeting_title": h.get("meeting_title"),
        "meeting_date": h.get("meeting_date"),
        "speaker_id": h.get("speaker_id"),
        "start_s": h.get("start_s"),
        "end_s": h.get("end_s"),
        "timestamp": _fmt_ts(h.get("start_s")),
        "jump_target": _jump_target(h.get("meeting_id"), h["segment_id"]),
        "snippet": (h.get("text") or "")[:200],
        "source_kind": h.get("source_kind", "meeting"),
        "file_id": h.get("file_id"),
        "file_name": h.get("file_name"),
        "locator": h.get("locator"),
    }


def _format_history(history: list[dict] | None, max_chars: int = 6000) -> str:
    """Render a small, clearly non-evidentiary conversation context."""
    if not history:
        return ""
    lines: list[str] = []
    total = 0
    for turn in history[-12:]:
        role = "Nutzer" if turn.get("role") == "user" else "Assistent"
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        line = f"{role}: {content[:2000]}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def rag_answer(query: str, hits: list[dict], llm: LLMEngine,
               max_tokens: int | None = None,
               history: list[dict] | None = None) -> dict:
    """Answer `query` grounded in `hits`. Raises ``LLMError`` on LLM failure.

    Returns ``{"answer", "sources", "citations", "grounded", ...}``. ``sources``
    are retrieved segments with cited hits listed first. ``grounded`` is True
    **only** when at least one citation resolves to an existing hit; otherwise
    the answer is replaced with an explicit "no sufficient evidence" message
    and ``grounded`` is False.
    """
    if not hits:
        return {
            "answer": _NO_EVIDENCE,
            "sources": [],
            "citations": [],
            "grounded": False,
            "context_chars": 0,
        }

    valid_ids = {h["segment_id"] for h in hits if h.get("segment_id")}
    hits_by_id = {h["segment_id"]: h for h in hits if h.get("segment_id")}

    context = build_context(hits)
    history_text = _format_history(history)
    conversation = (
        "Früherer Gesprächsverlauf (nur zur Auflösung von Rückfragen, "
        "keine Belegquelle):\n" + history_text + "\n\n"
        if history_text else ""
    )
    prompt = f"{conversation}Kontext:\n{context}\n\nFrage: {query}\n\nAntwort:"
    result: LLMResult = llm.complete(prompt, system=_SYSTEM,
                                     max_tokens=max_tokens)
    answer = visible_answer(result.text or "")

    # 1) The model explicitly declined.
    if answer.strip().lower() == _NOT_ANSWERED.lower():
        return {
            "answer": _NO_EVIDENCE,
            "sources": [],
            "citations": [],
            "grounded": False,
            "context_chars": len(context),
            "model": result.model,
        }

    # 2) Validate citations: only ids that exist in the retrieved hits count.
    citations = _extract_citations(answer)
    valid_citations = [c for c in citations if c in valid_ids]

    if not valid_citations:
        # The model produced text but cited nothing resolvable -> NOT grounded.
        # Never present an ungrounded answer as an answer.
        return {
            "answer": _NO_EVIDENCE,
            "sources": [],
            "citations": [],          # none valid
            "invalid_citations": [c for c in citations if c not in valid_ids],
            "grounded": False,
            "context_chars": len(context),
            "model": result.model,
        }

    # 3) Grounded: sources are exactly the validly-cited segments, cited order
    # first, then any remaining retrieved context (all still real segments).
    cited_hits = [hits_by_id[c] for c in valid_citations]
    rest = [h for h in hits if h["segment_id"] not in set(valid_citations)]
    sources = [_source_from_hit(h) for h in cited_hits + rest]

    return {
        "answer": answer,
        "sources": sources,
        "citations": valid_citations,
        "invalid_citations": [c for c in citations if c not in valid_ids],
        "grounded": True,
        "context_chars": len(context),
        "model": result.model,
    }
