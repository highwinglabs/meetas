"""Local, deterministic title/tag derivation for meetings (Phase 7).

No LLM, no network: the auto *title* comes from the analysis ``kurzfassung``
(first entry, cleaned and truncated) and auto *tags* are the most frequent
content words across the themen / decisions / risks / open-questions sections,
with a small German stopword filter. Everything is derived strictly from the
stored, validated analysis so nothing is invented.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List

# A compact German (+ a little English) stopword set for tag extraction; each
# entry appears exactly once (L20). Content words are kept.
_STOPWORDS = {
    "der", "die", "das", "und", "oder", "aber", "ist", "sind", "war", "waren",
    "ein", "eine", "einen", "einem", "einer", "dem", "den", "des", "mit", "auf",
    "für", "von", "zu", "im", "in", "an", "aus", "bei", "nach", "über", "unter",
    "wird", "werden", "kann", "können", "muss", "müssen", "soll", "sollen",
    "will", "wollen", "hat", "haben", "sich", "nicht", "auch", "als", "gibt",
    "am", "um", "wie", "wenn", "dass", "denn", "so", "dann", "noch", "nur",
    "sehr", "mehr", "viel", "viele", "alle", "all", "wir", "ihr", "sie", "es",
    "bzw", "etc", "usw", "z.b", "z.b.",
    "the", "and", "that", "for", "with", "are", "was", "were", "this", "these",
    "have", "has", "had", "would", "can", "could", "should",
}

_WORD_RE = re.compile(r"[a-zA-ZäöüÄÖÜß0-9]+")
_MIN_TAG_LEN = 3
_MAX_TITLE_CHARS = 90


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def auto_title(analysis: Dict[str, Any]) -> str | None:
    """Derive an auto title from the first ``kurzfassung`` entry (local only)."""
    for entry in analysis.get("kurzfassung") or []:
        if isinstance(entry, dict):
            t = _clean(entry.get("text") or "")
            if t:
                if len(t) > _MAX_TITLE_CHARS:
                    t = t[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",;:")
                return t
    return None


def auto_tags(analysis: Dict[str, Any], limit: int = 6) -> List[str]:
    """Derive up to ``limit`` tags from themen/decisions/risks content words."""
    sections = ("themen", "entscheidungen", "risiken", "offene_fragen")
    counter: Counter = Counter()
    for key in sections:
        for entry in analysis.get(key) or []:
            if isinstance(entry, dict):
                for m in _WORD_RE.findall(_clean(entry.get("text") or "")):
                    w = m.lower()
                    if len(w) < _MIN_TAG_LEN or w in _STOPWORDS:
                        continue
                    counter[w] += 1
    tags = [w for w, _ in counter.most_common(limit) if w not in _STOPWORDS]
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: List[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def analysis_from_content(content: str):
    """Parse stored analysis content into a dict (None when unparsable)."""
    from core.analysis.schema import extract_json
    data = extract_json(content or "")
    return data if isinstance(data, dict) else None
