"""Deterministic in-process LLM for development and tests.

Guarantees: **no network, no model, no real LLM request.** By default it
returns a *valid* structured JSON analysis (all nine areas, sources parsed from
the prompt) so the full parse/validate/store pipeline can be verified without a
server. For RAG chat prompts (context blocks marked with ``seg:<id>``) it
returns a short answer that cites the first real segment id, so the grounded
answer/citation pipeline can be verified deterministically as well.

Regler für Tests:
- ``fail=True`` -> every call raises ``LLMError``.
- ``busy=True`` -> every call raises ``ServerBusyError`` (exercises the API's
  busy mapping). The real client's wait/retry is tested separately with
  ``httpx.MockTransport`` (see ``tests/test_llm_provider.py``).
- ``invalid_once=True`` -> first call returns invalid/incomplete JSON, the
  second (self-correction) call returns valid JSON (exercises the one-shot fix).
- ``always_invalid=True`` -> every call returns non-JSON (exercises the clear
  "fail after one fix" path; nothing is stored).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from core.llm.base import LLMEngine, LLMError, LLMResult, ServerBusyError


class MockLLM(LLMEngine):
    model_name = "mock-llm"

    def __init__(
        self,
        text: Optional[str] = None,
        fail: bool = False,
        busy: bool = False,
        delay: float = 0.0,
        invalid_once: bool = False,
        always_invalid: bool = False,
    ) -> None:
        self._fixed = text
        self.fail = fail
        self.busy = busy
        self.delay = delay
        self.invalid_once = invalid_once
        self.always_invalid = always_invalid
        self.calls = 0
        self.last_prompt: Optional[str] = None
        self.last_system: Optional[str] = None

    def is_available(self) -> bool:
        return True

    def complete(self, prompt: str, system: Optional[str] = None, **opts: Any) -> LLMResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        if self.delay:
            time.sleep(self.delay)
        if self.busy:
            raise ServerBusyError("Mock-LLM: Server ist belegt (simuliert).")
        if self.fail:
            raise LLMError("Mock-LLM: simulierter Analyse-Fehler.")
        if self.always_invalid:
            content = "Das ist leider keine gültige Analyse, sondern nur ein Satz ohne JSON."
        elif self.invalid_once and self.calls == 1:
            # Truncated / incomplete JSON on the first attempt.
            content = '{"kurzfassung": [{"text": "unvollständig"'
        elif self._fixed is not None:
            content = self._fixed
        elif "seg:" in (prompt or ""):
            # RAG chat context: answer grounded in the first cited segment.
            content = self._rag_answer(prompt)
        else:
            content = self._valid_json(prompt)
        return LLMResult(
            text=content,
            model=self.model_name,
            usage={"prompt_chars": len(prompt or "")},
            raw={"mock": True, "system": system},
        )

    @staticmethod
    def _valid_json(prompt: str) -> str:
        """Build a valid, schema-complete analysis. Sources are parsed from the
        ``[S<n> | Sprecher: X | ts]`` markers in the prompt so the output always
        passes validation against the real segment ids."""
        ids = re.findall(r"\[(S\d+)\s*\|", prompt) or ["S1"]
        first = ids[0]
        second = ids[1] if len(ids) > 1 else ids[0]
        m = re.search(r"\[S1\s*\|\s*Sprecher:\s*([^|]+?)\s*\|\s*([^\]]+)\]", prompt)
        sp1 = m.group(1).strip() if m else "Sprecher 1"
        ts1 = m.group(2).strip() if m else "00:00:00-00:00:01"
        ts2 = "00:00:01-00:00:02"
        sp2 = "Sprecher 2"

        def q(sid: str, sp: str, ts: str) -> dict:
            return {"segment_id": sid, "sprecher": sp, "timestamp": ts}

        data = {
            "kurzfassung": [
                {"text": "(mock) Kurzfassung der Sitzung.", "quellen": [q(first, sp1, ts1)]},
            ],
            "themen": [
                {"text": "(mock) Thema 1.", "quellen": [q(first, sp1, ts1)]},
            ],
            "entscheidungen": [
                {"text": "(mock) Entscheidung 1.", "quellen": [q(first, sp1, ts1)]},
            ],
            "aufgaben": [
                {"text": "(mock) Aufgabe 1.",
                 "verantwortlich": "nicht angegeben", "deadline": "nicht angegeben",
                 "quellen": [q(second, sp2, ts2)]},
            ],
            "offene_fragen": [
                {"text": "(mock) Offene Frage 1.", "quellen": [q(first, sp1, ts1)]},
            ],
            "naechste_schritte": [
                {"text": "(mock) Nächster Schritt.", "quellen": [q(second, sp2, ts2)]},
            ],
            "risiken": [],
            "wichtige_fakten": [
                {"text": "(mock) Fakt 1.", "quellen": [q(first, sp1, ts1)]},
            ],
            "follow_ups": [
                {"text": "(mock) Follow-up 1.", "quellen": [q(first, sp1, ts1)]},
            ],
        }
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _rag_answer(prompt: str) -> str:
        """Build a grounded chat answer for a RAG context prompt.

        The context blocks from :func:`core.search.rag.build_context` look like
        ``[1] (seg:<id> | ...)\\n<text>``. The answer cites the first real
        segment id so the citation-validation path grants ``grounded=True``.
        """
        m = re.search(
            r"\[\d+\]\s*\(seg:([A-Za-z0-9]+)[^\n]*\)\n(.+)", prompt or "")
        if m:
            text = " ".join(m.group(2).split())
            # An empty segment line would swallow the next context header.
            if text and not text.startswith("[") and not text.startswith("Frage:"):
                return f"Laut Kontext: „{text[:160]}“ [seg:{m.group(1)}]."
        ids = re.findall(r"seg:([A-Za-z0-9]+)", prompt or "")
        if ids:
            return f"Zum Kontext lässt sich festhalten, dass er dies belegt. [seg:{ids[0]}]."
        return "Nicht im Transkript beantwortet."
