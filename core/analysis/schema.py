"""Structured, validated meeting-analysis schema (Phase 3).

The analysis is a **mandatory JSON structure** with nine fixed areas. Every
statement carries at least one *source* (``segment_id``, ``sprecher``,
``timestamp``) so nothing is asserted without a traceable anchor in the
transcript. Responsible persons / deadlines that the transcript does not state
are normalised to the exact sentinel ``"nicht angegeben"`` -- they are never
invented.

:func:`validate_analysis` is strict: it returns the normalised structure on
success, or a list of human-readable (German) error strings. The processor uses
those errors to ask the LLM to *fix its own output once*; if the second attempt
is still invalid the analysis fails clearly and nothing is stored.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

NOT_GIVEN = "nicht angegeben"

# (json_key, German title, extra_fields). Order is the canonical output order.
SECTIONS: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("kurzfassung", "Kurzfassung", ()),
    ("themen", "Themen", ()),
    ("entscheidungen", "Entscheidungen", ()),
    ("aufgaben", "Aufgaben / Action Items", ("verantwortlich", "deadline")),
    ("offene_fragen", "Offene Fragen", ()),
    ("naechste_schritte", "Naechste Schritte", ()),
    ("risiken", "Risiken / Probleme", ()),
    ("wichtige_fakten", "Wichtige Fakten", ()),
    ("follow_ups", "Follow-ups", ()),
]
SECTION_KEYS: List[str] = [k for k, _, _ in SECTIONS]
SECTION_TITLES: Dict[str, str] = {k: t for k, t, _ in SECTIONS}

SYSTEM_PROMPT = (
    "Du bist ein praeziser, ruehrender Meeting-Protokollant. Du analysierst ein "
    "deutsches Transkript und gibst AUSSCHLIESSLICH ein einziges, gueltiges "
    "JSON-Objekt aus: kein Markdown, keine Code-Zaune, kein Text davor oder "
    "danach. Du erfindest KEINE Fakten, Namen, Zahlen, Daten oder Fristen. Alles, "
    "was das Transkript nicht ausdruemlich nennt, gibst du als \u201enicht angegeben\u201c an. "
    "Jede Aussage braucht mindestens eine Quelle aus dem Transkript "
    "(segment_id, sprecher, timestamp). Du zitierst ausschliesslich Segment-IDs, "
    "die im Transkript vorkommen (z. B. S1, S2, ...)."
)


def system_prompt_for_template(template: Optional[str]) -> str:
    """Return a safe built-in analysis style without allowing fact rules to be
    replaced by a UI label.

    Templates only change emphasis; the evidence/no-invention contract remains
    part of the base prompt for every variant.
    """
    key = (template or "standard").strip().lower()
    suffix = {
        "compact": " Arbeite besonders knapp und vermeide Wiederholungen.",
        "audit": " Lege besonderen Wert auf Risiken, Nachweise, offene Punkte und konkrete Zusagen.",
        "action_items": " Lege besonderen Wert auf Aufgaben, Verantwortliche, Fristen und deren Quellen.",
    }.get(key, "")
    return SYSTEM_PROMPT + suffix

FIX_SYSTEM_PROMPT = (
    "Du korrigierst fehlerhaftes JSON. Du gibst AUSSCHLIESSLICH ein gueltiges, "
    "vollstaendiges JSON-Objekt aus: kein Markdown, keine Code-Zaune, kein "
    "anderer Text. Du erfindest keine Fakten und verweist nur auf Segment-IDs aus "
    "dem vorgegebenen Transkript."
)


def fmt_time(seconds: float) -> str:
    try:
        secs = int(max(0, seconds or 0))
    except (TypeError, ValueError):
        return "00:00:00"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_seg_rows(
    segments: Sequence[Tuple[float, float, Optional[str], str]],
) -> List[Dict[str, Any]]:
    """Normalise ``(start_s, end_s, speaker, text)`` tuples into prompt rows with
    positional, easily-citable ``S<n>`` segment ids (1-based, playback order)."""
    rows: List[Dict[str, Any]] = []
    for i, (start, end, speaker, text) in enumerate(segments, start=1):
        rows.append({
            "sid": f"S{i}",
            "start_s": float(start or 0.0),
            "end_s": float(end or 0.0),
            "speaker": (speaker or "").strip() or "Unbekannt",
            "text": (text or "").strip(),
        })
    return rows


def segment_ids(rows: Sequence[Dict[str, Any]]) -> set:
    return {r["sid"] for r in rows}


def _line(r: Dict[str, Any]) -> str:
    return (
        f"[{r['sid']} | Sprecher: {r['speaker']} | "
        f"{fmt_time(r['start_s'])}-{fmt_time(r['end_s'])}] {r['text']}"
    )


def build_user_prompt(title: str, rows: Sequence[Dict[str, Any]],
                      lang: Optional[str] = None) -> str:
    transcript = "\n".join(_line(r) for r in rows) if rows else "(leeres Transkript)"
    lang_label = (lang or "unbekannt").strip() or "unbekannt"
    keys = ", ".join(k for k, _, _ in SECTIONS)
    base_keys = ", ".join('"%s"' % k for k, _, extra in SECTIONS if not extra)
    return "\n".join([
        "Analysiere das Transkript und gib NUR das folgende JSON-Objekt aus.",
        "",
        f"Meeting: {title}",
        f"Sprache: {lang_label}",
        "",
        "Pflicht-Bereiche - alle 9 Schlüssel MÜSSEN vorhanden sein (leeres Array [] "
        "falls in diesem Meeting nichts davon vorkommt):",
        keys,
        "",
        "Aufbau je Eintrag:",
        f"- {base_keys}: {{\"text\": \"...\", \"quellen\": [ ... ]}}",
        '- "aufgaben": {{"text": "...", "verantwortlich": "...", "deadline": "...", '
        '"quellen": [ ... ]}}',
        f'  (verantwortlich/deadline exakt "{NOT_GIVEN}" wenn im Transkript nicht genannt)',
        "",
        "Regeln:",
        '- Jeder Eintrag braucht "text" (nicht leer) und "quellen" (mindestens 1 Objekt).',
        '- Jedes Objekt in "quellen" hat exakt: "segment_id", "sprecher", "timestamp" '
        "(timestamp im Format HH:MM:SS-HH:MM:SS).",
        '- "segment_id" NUR aus dem Transkript (S1, S2, ...). Keine unbekannten IDs erfinden.',
        "- Keine erfundenen Fakten, Namen, Zahlen oder Fristen.",
        "- Ausgabe: NUR das JSON-Objekt, sonst absolut nichts (kein Markdown, keine Code-Zaune).",
        "",
        "Beispiel (Struktur; Inhalte ersetzen):",
        "{",
        '  "kurzfassung": [{"text": "...", "quellen": [{"segment_id": "S1", '
        '"sprecher": "Anna", "timestamp": "00:00:00-00:00:04"}]}],',
        '  "aufgaben": [{"text": "...", "verantwortlich": "Ben", "deadline": "Ende der Woche", '
        '"quellen": [{"segment_id": "S2", "sprecher": "Ben", "timestamp": "00:00:04-00:00:09"}]}],',
        '  "risiken": []',
        "}",
        "",
        "Transkript:",
        transcript,
        "",
        "Gib jetzt NUR das JSON-Objekt aus.",
    ])


def build_fix_prompt(errors: Sequence[str], raw: str,
                     rows: Sequence[Dict[str, Any]]) -> str:
    err_list = "\n".join(f"- {e}" for e in list(errors)[:20])
    valid = ", ".join(r["sid"] for r in rows)
    transcript = "\n".join(_line(r) for r in rows) if rows else "(leeres Transkript)"
    keys = ", ".join(k for k, _, _ in SECTIONS)
    return "\n".join([
        "Deine vorige Antwort war ungültiges oder unvollständiges JSON. Korrigiere sie.",
        "Fehler:",
        err_list,
        "",
        "Korrekturen:",
        f"- Alle 9 Pflicht-Bereiche vorhanden: {keys} (leeres Array [] ist erlaubt).",
        '- Jeder Eintrag mit "text" und mindestens einer "quelle" '
        '("segment_id", "sprecher", "timestamp").',
        f"- Erlaubte Werte fuer segment_id: {valid}. Keine anderen IDs verwenden.",
        f'- Unbekannte Verantwortliche/Deadlines exakt: "{NOT_GIVEN}".',
        "- NUR das korrigierte, vollständige JSON-Objekt ausgeben (kein Markdown, keine Code-Zaune).",
        "",
        "Ursprüngliche (ungültige) Ausgabe:",
        raw or "(leer)",
        "",
        "Transkript:",
        transcript,
        "",
        "Gib jetzt NUR das korrigierte JSON-Objekt aus.",
    ])


# Qwen3-style chain-of-thought blocks emitted inline in the answer content.
_THINK_RE = re.compile(r"think[\s\S]*?/think", re.IGNORECASE)
# Markdown code fences: ```` ``` ````, ```` ```json ````, ```` ```JSON ````, etc.
_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*")


def _strip_thinking(text: str) -> str:
    """Remove ``...`` blocks (they may contain braces / example JSON)."""
    return _THINK_RE.sub("", text)


def _strip_fences(text: str) -> str:
    """Remove Markdown code-fence markers so a fenced object is plain JSON."""
    return _FENCE_RE.sub("", text)


def _balanced_object(text: str, start: int) -> Optional[str]:
    """Return the balanced ``{...}`` slice starting at ``text[start] == '{'``,
    or ``None`` if it never closes. Respects JSON string literals so braces
    inside string values do not affect the depth."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _repair_unescaped_quotes(text: str) -> str:
    """Escape straight double quotes that appear *inside* JSON string values.

    Local models sometimes embed quotes in a ``text`` field (e.g. ``"Die Frage
    "Wie geht's?" wurde gestellt"``) without escaping them, which silently ends
    the JSON string early and makes the whole object unparseable. A double quote
    that does NOT close a string is one whose next non-whitespace character is
    not a JSON structural token (`,` `}` `]` `:` or end-of-input). Those are
    re-emitted as ``\\"``; genuine terminators are left untouched. This runs
    only as a fallback after the plain parse already failed, so valid output is
    never altered."""
    out: List[str] = []
    in_str = False
    esc = False
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
            i += 1
            continue
        if esc:
            out.append(c)
            esc = False
            i += 1
            continue
        if c == "\\":
            out.append(c)
            esc = True
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",}]::":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_object(chunk: str) -> Any:
    """Parse a balanced chunk; fall back to quote-repair for LLM output."""
    for candidate in (chunk, _repair_unescaped_quotes(chunk)):
        try:
            obj = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _candidate_objects(text: str) -> List[Any]:
    """Parse every balanced ``{...}`` region that yields a JSON object."""
    out: List[Any] = []
    i = text.find("{")
    while i != -1:
        obj_str = _balanced_object(text, i)
        if obj_str is not None:
            obj = _parse_object(obj_str)
            if obj is not None:
                out.append(obj)
        i = text.find("{", i + 1)
    return out


def _analysis_score(obj: Any) -> Tuple[int, int]:
    """Prefer the object that looks most like the analysis: count of the nine
    canonical section keys, then raw size. Ties -> the larger one."""
    if not isinstance(obj, dict):
        return (-1, 0)
    n_keys = sum(1 for k in SECTION_KEYS if k in obj)
    return (n_keys, len(json.dumps(obj, ensure_ascii=False)))


def extract_json(text: Optional[str]) -> Any:
    """Best-effort extraction of the analysis JSON object from a model answer.

    Robust against the shapes local Qwen models actually emit:
      * a bare JSON object,
      * a ```json ... ``` Markdown fence,
      * a leading/trailing ``...`` chain-of-thought block,
      * explanatory prose before or after the object,
      * arbitrary whitespace,
      * unescaped straight quotes inside a ``text`` value (repaired before the
        final parse; valid output is never touched).

    Strategy: strip thinking blocks and fence markers, then parse every balanced
    ``{...}`` region and return the one that best matches the analysis schema
    (most of the nine section keys, then the largest). Returns ``None`` if no
    valid JSON object can be found. The returned object is still validated by
    :func:`validate_analysis` -- this only makes *recognition* robust.
    """
    if not text:
        return None
    t = _strip_fences(_strip_thinking(text.strip()))
    candidates = _candidate_objects(t)
    best = max(candidates, key=_analysis_score) if candidates else None
    if best is None or _analysis_score(best)[0] == 0:
        # Unescaped straight quotes inside a ``text`` value desync the plain
        # balanced-scan (it then only sees inner objects, no section keys).
        # Repair them -- a no-op on already-valid JSON -- and scan again.
        repaired = _candidate_objects(_repair_unescaped_quotes(t))
        if repaired:
            best = max(repaired, key=_analysis_score)
    return best


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate_analysis(data: Any, valid_segment_ids: set) -> Tuple[Optional[dict], List[str]]:
    """Validate + normalise the raw parsed JSON.

    Returns ``(normalised, errors)``. On success ``errors`` is empty and
    ``normalised`` has all nine sections. On failure ``normalised`` is ``None``
    and ``errors`` lists the concrete problems (used for the one-shot fix).
    """
    if not isinstance(data, dict):
        return None, ["Antwort ist kein JSON-Objekt (oder leer/ungültiges JSON)"]

    errors: List[str] = []
    normalised: Dict[str, Any] = {}

    for key, title, extra in SECTIONS:
        raw = data.get(key)
        if key not in data:
            errors.append(f"fehlender Bereich '{title}' (Schlüssel '{key}')")
            normalised[key] = []
            continue
        if not isinstance(raw, list):
            errors.append(f"'{title}' muss eine Liste sein")
            normalised[key] = []
            continue

        entries: List[Dict[str, Any]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                errors.append(f"{title}[{i}]: Eintrag ist kein Objekt")
                continue

            text = item.get("text")
            if not _is_nonempty_str(text):
                errors.append(f"{title}[{i}]: 'text' fehlt oder ist leer")

            quellen = item.get("quellen")
            if not isinstance(quellen, list) or len(quellen) == 0:
                errors.append(
                    f"{title}[{i}]: 'quellen' fehlt oder ist leer "
                    "(jede Aussage braucht mindestens eine Quelle)"
                )
                cleaned: List[Dict[str, str]] = []
            else:
                cleaned = []
                for j, src in enumerate(quellen):
                    if not isinstance(src, dict):
                        errors.append(f"{title}[{i}].quellen[{j}]: keine Quelle-Objekt")
                        continue
                    sid = src.get("segment_id")
                    if not _is_nonempty_str(sid):
                        errors.append(f"{title}[{i}].quellen[{j}]: 'segment_id' fehlt")
                    elif sid.strip() not in valid_segment_ids:
                        errors.append(
                            f"{title}[{i}].quellen[{j}]: segment_id '{sid.strip()}' "
                            "existiert nicht im Transkript (Quelle erfinden?)"
                        )
                    sprecher = src.get("sprecher")
                    if not _is_nonempty_str(sprecher):
                        errors.append(f"{title}[{i}].quellen[{j}]: 'sprecher' fehlt")
                    ts = src.get("timestamp")
                    if not _is_nonempty_str(ts):
                        errors.append(f"{title}[{i}].quellen[{j}]: 'timestamp' fehlt")
                    cleaned.append({
                        "segment_id": sid.strip() if _is_nonempty_str(sid) else "",
                        "sprecher": sprecher.strip() if _is_nonempty_str(sprecher) else "",
                        "timestamp": ts.strip() if _is_nonempty_str(ts) else "",
                    })

            entry: Dict[str, Any] = {
                "text": text.strip() if _is_nonempty_str(text) else "",
                "quellen": cleaned,
            }
            if key == "aufgaben":
                for fld in extra:
                    v = item.get(fld)
                    entry[fld] = v.strip() if _is_nonempty_str(v) else NOT_GIVEN
            entries.append(entry)

        normalised[key] = entries

    return (normalised, []) if not errors else (None, errors)


def render_markdown(analysis: Optional[Dict[str, Any]],
                    section_keys: Optional[List[str]] = None) -> Optional[str]:
    """Render the normalised analysis as readable Markdown for UI/CLI/export.

    ``section_keys`` optionally restricts the output to the given sections
    (output order always follows SECTIONS). String entries from unnormalised
    legacy records are rendered as plain bullets; non-dict sources are ignored.
    """
    if not analysis:
        return None
    keys = section_keys or list(SECTION_KEYS)
    out: List[str] = []
    for key, title, _ in SECTIONS:
        if key not in keys:
            continue
        out.append(f"## {title}")
        entries = analysis.get(key, []) or []
        if not entries:
            out.append(f"- {NOT_GIVEN}")
        for e in entries:
            if isinstance(e, str):
                out.append(f"- {e}")
                continue
            out.append(f"- {e.get('text', '')}")
            if key == "aufgaben":
                out.append(
                    f"  - Verantwortlich: {e.get('verantwortlich', NOT_GIVEN)} | "
                    f"Deadline: {e.get('deadline', NOT_GIVEN)}"
                )
            for q in e.get("quellen", []) or []:
                if isinstance(q, dict):
                    out.append(
                        f"  - Quelle: {q.get('segment_id', '?')} · "
                        f"{q.get('sprecher', '?')} · {q.get('timestamp', '?')}"
                    )
        out.append("")
    return "\n".join(out).rstrip() + "\n"
