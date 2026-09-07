"""Structured, validated meeting-analysis schema (Phase 3).

The analysis is a **mandatory JSON structure** with nine fixed areas.
Statements *may* carry sources: the model cites them as a list of positional
``S<n>`` ids (``"quellen": ["S3", "S7"]``). The validator resolves every
cited id against the transcript rows and stores the fully sourced entry
(sid, real segment id, speaker, timestamp, snippet) -- the model can never
invent speaker or timestamp, and unknown ids are dropped silently. Sources
remain **optional**: small local models do not always emit them, and their
absence must never fail a valid summary. Responsible persons / deadlines
that the transcript does not state are normalised to the exact sentinel
``"nicht angegeben"`` -- they are never invented.

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

# Every value that means "not specified" in stored/produced analysis data. The
# legacy German sentinel dominates; the short forms the UI/API may also emit are
# recognised too. The English forms appear once an analysis is written in
# English. Defined in ONE place so missing-value detection never drifts.
LEGACY_MISSING = {"", "n/a", "-", "nicht angegeben", "not specified", "not given"}


def is_missing(value: Any) -> bool:
    """Return True when ``value`` represents "not specified".

    Central predicate for missing analysis values: ``None``, empty or
    whitespace-only strings, and any of the legacy sentinel forms in
    :data:`LEGACY_MISSING` (compared case-insensitively). Anything else --
    including real names, dates and numbers -- is *not* missing, so a genuine
    value is never mistaken for an absent one.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in LEGACY_MISSING


# (json_key, German title, extra_fields). Order is the canonical output order.
#
# The ``json_key`` values are **stable, language-neutral technical identifiers**,
# not display text. They are the on-disk contract shared by the stored
# ``Analysis.content`` JSON, the UI (``ui/src/types.ts``), task extraction
# (``core/tasks.py``), analytics, export and the mock LLM. Do NOT rename them:
# renaming would silently invalidate every stored analysis and desync all those
# readers. The human-readable names live in the German ``title`` column and are
# the only localisable part of this table.
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
    "was das Transkript nicht ausdruemlich nennt, gibst du als \u201enicht angegeben\u201c an."
)


# German adjectives describing the transcript's own language inside the
# (German-language) system prompt. Only this descriptor adapts to the meeting
# language; the prompt itself stays German. Unknown or absent languages fall
# back to the historical German default, so German analyses are unchanged.
_TRANSCRIPT_LANG_ADJ = {
    "de": "deutsches",
    "en": "englisches",
    "fr": "französisches",
    "es": "spanisches",
    "it": "italienisches",
    "pl": "polnisches",
}


def _transcript_lang_adj(lang: Optional[str]) -> str:
    key = (lang or "").strip().lower()
    if not key:
        return "deutsches"
    code = key.split("-", 1)[0].split("_", 1)[0]
    return _TRANSCRIPT_LANG_ADJ.get(code, "deutsches")


# German display names for the *output* language directive. Only languages a
# local model reliably writes are offered; anything else resolves to no
# directive (historical implicit-German behaviour).
_OUTPUT_LANG_NAMES = {
    "de": "Deutsch",
    "en": "Englisch",
    "fr": "Französisch",
    "es": "Spanisch",
    "it": "Italienisch",
    "pl": "Polnisch",
}


def _output_lang_name(code: Optional[str]) -> Optional[str]:
    key = (code or "").strip().lower()
    if not key:
        return None
    c = key.split("-", 1)[0].split("_", 1)[0]
    return _OUTPUT_LANG_NAMES.get(c)


def resolve_output_lang(analysis_language: Optional[str],
                        transcript_lang: Optional[str]) -> Optional[str]:
    """Resolve the concrete language the analysis *output* must use.

    ``analysis_language`` is the per-meeting choice. ``"wie_transkript"`` (or
    empty / ``None`` / ``"auto"``) means "match the transcript"; a concrete code
    such as ``"de"`` or ``"en"`` forces that language. Returns a stripped code,
    or ``None`` when no concrete language is known (transcript unknown and the
    choice is "wie_transkript") -- in that case the prompt carries no output
    directive and the historical implicit-German behaviour applies.
    """
    raw = (analysis_language or "").strip()
    if raw in ("", "auto", "wie_transkript"):
        return (transcript_lang or "").strip() or None
    return raw or None


def _output_lang_directive(output_lang: Optional[str]) -> str:
    """Explicit output-language instruction, or empty for the implicit default."""
    name = _output_lang_name(output_lang)
    if not name:
        return ""
    return (
        " Gib ALLE Textinhalte (Kurzfassung, Themen, Entscheidungen, Aufgaben, "
        "Offene Fragen, Naechste Schritte, Risiken, Wichtige Fakten, Follow-ups "
        "sowie jedes 'text'-Feld) auf " + name
        + " aus. Uebersetze den Inhalt frei, halte aber Namen, Zahlen und "
        "Zitate exakt bei."
    )


# ---------------------------------------------------------------------------
# Localised DISPLAY strings for analysis rendering (de/en).
#
# Only the two output languages reachable via the per-meeting analysis-language
# control are provided; anything else (incl. NULL / legacy) falls back to
# German, the historical default. These are display-only: the LLM contract and
# the stored sentinel stay NOT_GIVEN ("nicht angegeben") -- they are mapped to
# the language at render time, never rewritten in storage.
# ---------------------------------------------------------------------------
_SECTION_TITLES_BY_LANG: Dict[str, Dict[str, str]] = {
    "de": {key: title for key, title, _ in SECTIONS},
    "en": {
        "kurzfassung": "Summary",
        "themen": "Topics",
        "entscheidungen": "Decisions",
        "aufgaben": "Action Items",
        "offene_fragen": "Open Questions",
        "naechste_schritte": "Next Steps",
        "risiken": "Risks / Issues",
        "wichtige_fakten": "Key Facts",
        "follow_ups": "Follow-ups",
    },
}
_MISSING_TEXT_BY_LANG: Dict[str, str] = {"de": NOT_GIVEN, "en": "not specified"}
_ANALYSIS_LABELS_BY_LANG: Dict[str, Dict[str, str]] = {
    "de": {"verantwortlich": "Verantwortlich", "deadline": "Deadline", "quelle": "Quelle", "quellen": "Quellen"},
    "en": {"verantwortlich": "Owner", "deadline": "Deadline", "quelle": "Source", "quellen": "Sources"},
}


def _lang_code(lang: Optional[str]) -> str:
    """Normalise a stored/derived language to a supported key; fallback "de"."""
    key = (lang or "").strip().lower().split("-", 1)[0].split("_", 1)[0]
    return key if key in _SECTION_TITLES_BY_LANG else "de"


def section_title(key: str, lang: Optional[str] = None) -> str:
    """Display title for an analysis section in the given language."""
    return _SECTION_TITLES_BY_LANG[_lang_code(lang)].get(key, SECTION_TITLES.get(key, key))


def missing_text(lang: Optional[str] = None) -> str:
    """The 'not specified' placeholder in the given language (display only)."""
    return _MISSING_TEXT_BY_LANG[_lang_code(lang)]


def analysis_label(name: str, lang: Optional[str] = None) -> str:
    """Small labels used inside the analysis rendering (display only)."""
    return _ANALYSIS_LABELS_BY_LANG[_lang_code(lang)].get(
        name, _ANALYSIS_LABELS_BY_LANG["de"].get(name, name)
    )


def system_prompt(lang: Optional[str] = None,
                  output_lang: Optional[str] = None) -> str:
    """Base analysis prompt with the transcript language made explicit.

    Only the single phrase naming the transcript's language changes. For a
    German (or unknown/absent) language this returns :data:`SYSTEM_PROMPT`
    unchanged, so existing German behaviour is preserved exactly. When a
    concrete ``output_lang`` is given, an explicit instruction to write all text
    content in that language is appended; otherwise the prompt is unchanged.
    """
    adj = _transcript_lang_adj(lang)
    base = SYSTEM_PROMPT.replace("deutsches Transkript", f"{adj} Transkript")
    return base + _output_lang_directive(output_lang)


def system_prompt_for_template(template: Optional[str],
                               lang: Optional[str] = None,
                               output_lang: Optional[str] = None) -> str:
    """Return a safe built-in analysis style without allowing fact rules to be
    replaced by a UI label.

    Templates only change emphasis; the evidence/no-invention contract remains
    part of the base prompt for every variant.
    """
    key = (template or "standard").strip().lower()
    suffix = {
        "compact": " Arbeite besonders knapp und vermeide Wiederholungen.",
        "audit": " Lege besonderen Wert auf Risiken, offene Punkte und konkrete Zusagen.",
        "action_items": " Lege besonderen Wert auf Aufgaben, Verantwortliche und Fristen.",
    }.get(key, "")
    return system_prompt(lang, output_lang) + suffix

FIX_SYSTEM_PROMPT = (
    "Du korrigierst fehlerhaftes JSON. Du gibst AUSSCHLIESSLICH ein gueltiges, "
    "vollstaendiges JSON-Objekt aus: kein Markdown, keine Code-Zaune, kein "
    "anderer Text. Du erfindest keine Fakten."
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
    segments: Sequence[tuple],
) -> List[Dict[str, Any]]:
    """Normalise ``(start_s, end_s, speaker, text[, segment_id])`` tuples into
    prompt rows with positional, easily-citable ``S<n>`` segment ids (1-based,
    playback order). The optional fifth element is the segment's real id; it
    is carried along for source resolution and never shown to the model."""
    rows: List[Dict[str, Any]] = []
    for i, seg in enumerate(segments, start=1):
        start, end, speaker, text = seg[0], seg[1], seg[2], seg[3]
        segment_id = seg[4] if len(seg) > 4 else None
        rows.append({
            "sid": f"S{i}",
            "segment_id": segment_id,
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
        f"- {base_keys}: {{\"text\": \"...\"}}",
        '- "aufgaben": {"text": "...", "verantwortlich": "...", "deadline": "..."}',
        f'  (verantwortlich/deadline exakt "{NOT_GIVEN}" wenn im Transkript nicht genannt)',
        "",
        "Regeln:",
        '- Jeder Eintrag braucht "text" (nicht leer).',
        "- Keine erfundenen Fakten, Namen, Zahlen oder Fristen.",
        '- Jeder Eintrag zitiert seine Belege: "quellen" ist eine Liste der S-Nummern '
        'aus dem Transkript, z.B. "quellen": ["S3", "S7"] (nur Nummern, die es im '
        'Transkript wirklich gibt; leeres Array [], wenn keine passende Stelle '
        'bestimmt werden kann).',
        "- Ausgabe: NUR das JSON-Objekt, sonst absolut nichts (kein Markdown, keine Code-Zaune).",
        "",
        "Beispiel (Struktur; Inhalte ersetzen):",
        "{",
        '  "kurzfassung": [{"text": "...", "quellen": ["S1"]}],',
        '  "aufgaben": [{"text": "...", "verantwortlich": "Ben", "deadline": "Ende der Woche", "quellen": ["S4"]}],',
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
    transcript = "\n".join(_line(r) for r in rows) if rows else "(leeres Transkript)"
    keys = ", ".join(k for k, _, _ in SECTIONS)
    return "\n".join([
        "Deine vorige Antwort war ungültiges oder unvollständiges JSON. Korrigiere sie.",
        "Fehler:",
        err_list,
        "",
        "Korrekturen:",
        f"- Alle 9 Pflicht-Bereiche vorhanden: {keys} (leeres Array [] ist erlaubt).",
        '- Jeder Eintrag braucht "text" (nicht leer).',
        '- "quellen" ist eine Liste von S-Nummern (z.B. ["S1"]) oder ein leeres Array.',
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


def _source_sid(ref: Any) -> Optional[str]:
    """Normalise one raw source reference to an ``S<n>`` id, or ``None``.

    Accepts the new plain form (``"S5"``) and the legacy dict form
    (``{"segment_id": "S5", ...}`` from older stored/produced data).
    """
    if isinstance(ref, str):
        cand = ref.strip()
    elif isinstance(ref, dict):
        raw = ref.get("segment_id")
        cand = raw.strip() if isinstance(raw, str) else ""
    else:
        cand = ""
    return cand or None


def _source_from_row(sid: str, row: Dict[str, Any]) -> Dict[str, str]:
    """Resolve a cited ``S<n>`` id into a fully sourced entry.

    Speaker, timestamp and snippet come exclusively from the transcript row,
    so the model can never invent them. ``segment_id`` is the segment's real
    id (for transcript jumps); without one (e.g. legacy rows) the S-id is kept.
    """
    text = (row.get("text") or "").strip()
    return {
        "sid": sid,
        "segment_id": row.get("segment_id") or sid,
        "sprecher": (row.get("speaker") or "").strip() or "Unbekannt",
        "timestamp": fmt_time(row.get("start_s", 0.0)),
        "snippet": text[:200],
    }


def validate_analysis(data: Any, rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[dict], List[str]]:
    """Validate + normalise the raw parsed JSON.

    ``rows`` are the transcript rows from :func:`to_seg_rows` (the ones the
    model saw): sources are resolved against them, so a cited id that does not
    exist in the shown transcript is dropped silently (never an error).

    Returns ``(normalised, errors)``. On success ``errors`` is empty and
    ``normalised`` has all nine sections. On failure ``normalised`` is ``None``
    and ``errors`` lists the concrete problems (used for the one-shot fix).
    """
    by_sid = {r["sid"]: r for r in rows}
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

            # Sources are optional (see module docstring): the summary must
            # work with models that omit them. Cited S-ids are resolved against
            # the transcript rows (speaker/timestamp/snippet come from the
            # data, not the model); unknown ids are dropped silently, so a
            # missing or bad source never fails the whole analysis.
            quellen = item.get("quellen")
            cleaned: List[Dict[str, str]] = []
            seen_sids = set()
            if isinstance(quellen, list):
                for ref in quellen:
                    sid = _source_sid(ref)
                    if sid is None or sid not in by_sid or sid in seen_sids:
                        continue
                    seen_sids.add(sid)
                    cleaned.append(_source_from_row(sid, by_sid[sid]))

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
                    section_keys: Optional[List[str]] = None,
                    lang: Optional[str] = None) -> Optional[str]:
    """Render the normalised analysis as readable Markdown for UI/CLI/export.

    ``section_keys`` optionally restricts the output to the given sections
    (output order always follows SECTIONS). String entries from unnormalised
    legacy records are rendered as plain bullets; non-dict sources are ignored.

    ``lang`` localises the section headings, the "not specified" placeholder and
    the action-item labels to the analysis output language. It defaults to
    German (the historical default) -- omitting it reproduces the exact output
    of the previous implementation.
    """
    if not analysis:
        return None
    keys = section_keys or list(SECTION_KEYS)
    missing = missing_text(lang)
    lbl = _ANALYSIS_LABELS_BY_LANG[_lang_code(lang)]
    out: List[str] = []
    numbered: List[Any] = []  # all sources in document order (global [n])
    for key, _title, _ in SECTIONS:
        if key not in keys:
            continue
        out.append(f"## {section_title(key, lang)}")
        entries = analysis.get(key, []) or []
        if not entries:
            out.append(f"- {missing}")
        for e in entries:
            if isinstance(e, str):
                out.append(f"- {e}")
                continue
            srcs = e.get("quellen")
            srcs = srcs if isinstance(srcs, list) else []
            numbered.extend(srcs)
            line = f"- {e.get('text', '')}"
            if srcs:
                # Global [n] references, consistent with the UI rendering.
                refs = " ".join(f"[{i}]" for i in
                                range(len(numbered) - len(srcs) + 1, len(numbered) + 1))
                line += " " + refs
            out.append(line)
            if key == "aufgaben":
                verantwortlich = e.get("verantwortlich")
                deadline = e.get("deadline")
                out.append(
                    f"  - {lbl['verantwortlich']}: "
                    f"{verantwortlich if not is_missing(verantwortlich) else missing} | "
                    f"{lbl['deadline']}: "
                    f"{deadline if not is_missing(deadline) else missing}"
                )
        out.append("")
    if numbered:
        out.append(f"## {lbl['quellen']}")
        for n, src in enumerate(numbered, start=1):
            if isinstance(src, dict):
                sp = (src.get("sprecher") or "").strip()
                ts = (src.get("timestamp") or "").strip()
                sn = (src.get("snippet") or "").strip()
            else:
                sp = ts = sn = ""
            head = f"{n}. {sp} \u00b7 {ts}" if (sp or ts) else f"{n}."
            out.append(f"{head} \u2014 \u201e{sn}\u201c" if sn else head)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
