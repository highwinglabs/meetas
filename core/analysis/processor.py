"""Meeting analysis (Phase 3): transcript -> validated structured JSON analysis.

The :class:`AnalysisProcessor` asks the (local) LLM engine to return a
**mandatory JSON structure** (nine fixed areas) for a stored transcript. The
answer is parsed and *strictly validated*:

* all nine areas must be present (empty lists allowed),
* sources are optional: well-formed sources citing a real segment are kept,
  missing or invalid ones are dropped (small local models often omit them),
* unknown responsible persons / deadlines are normalised to "nicht angegeben".

If the model returns invalid or incomplete JSON, it is asked **once** to fix its
own output (with the concrete error list). If the second attempt is still
invalid, the analysis fails clearly and **nothing is stored** -- we never invent
facts. The (slow) LLM call happens outside any open DB session; the resumable
``analyze`` job + meeting status are updated before and after it.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import timezone
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from sqlalchemy import select

from core.analysis import schema
from core.jobs.queue import JobQueue
from core.llm.base import (
    LLMCancelledError, LLMContextOverflowError, LLMEngine, LLMError, LLMResult,
)
from core.logging_setup import get_logger
from core.store.db import session_scope
from core.store.models import Analysis, Meeting, TranscriptSegment

log = get_logger("ma.analysis")

# Fallback transcript budget (characters) used when the model's context
# window is unknown -- keeps the prompt comfortably inside a small local
# model's window.
_MAX_TRANSCRIPT_CHARS = 24000
# Tokens reserved for the prompt scaffolding / instructions around the
# transcript, and a conservative characters-per-token rate for the prompt
# rows when converting the remaining input budget into a character limit.
# The rows are NOT plain prose: every line carries a structured prefix
# ("[S1 | Sprecher: X | 00:00:00.000-00:00:03.500] ") whose timestamps and
# punctuation tokenize at well under 3 chars/token -- measured ~2.3 for
# German. The rate must stay below that, otherwise the budget overestimates
# the prompt and the server rejects it with exceed_context_size_error.
_PROMPT_OVERHEAD_TOKENS = 4096
_CHARS_PER_TOKEN = 2.0
# Safety margin applied when shrinking the transcript after a real
# context-overflow rejection: target 90 % of the window, never 100 %.
_OVERFLOW_RETRY_MARGIN = 0.9


def transcript_char_budget(config: Any, window_tokens: Optional[int]) -> int:
    """Max transcript characters that fit into the model's context window.

    Reserves room for the prompt scaffolding and for the model's JSON answer
    (the configured ``llm_max_tokens``, at least 1/8 of the window), caps the
    prompt at the configured ``llm_max_prompt_tokens`` (a full-window prompt
    is legal but impractically slow to prefill locally), converts the
    remaining input budget to characters, and never goes below the safe
    historical default. A missing or too-small window yields the default.
    """
    try:
        window = int(window_tokens or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 16384:
        return _MAX_TRANSCRIPT_CHARS
    output_reserve = max(int(getattr(config, "llm_max_tokens", 0) or 0),
                        window // 8)
    usable = window - output_reserve - _PROMPT_OVERHEAD_TOKENS
    prompt_cap = int(getattr(config, "llm_max_prompt_tokens", 0) or 0)
    if prompt_cap > 0:
        # The cap bounds the *whole* prompt, so the scaffolding is subtracted.
        usable = min(usable, prompt_cap - _PROMPT_OVERHEAD_TOKENS)
    if usable < 8192:
        return _MAX_TRANSCRIPT_CHARS
    return max(_MAX_TRANSCRIPT_CHARS, int(usable * _CHARS_PER_TOKEN))


def build_prompt(
    title: str,
    segments: Sequence[tuple],
    lang: Optional[str] = None,
    kind: str = "summary",
    override_system: Optional[str] = None,
    output_lang: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> Tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the structured JSON analysis.

    ``segments`` is a sequence of ``(start_s, end_s, speaker_id, text)`` (or
    ``(..., real_segment_id)``) in playback order. The user prompt carries
    positional ``S<n>`` segment ids for citing sources. ``max_chars`` bounds
    the transcript (default: the safe historical limit). ``output_lang`` adds
    an explicit instruction to write all text content in that language.
    """
    rows = _trim(schema.to_seg_rows(segments),
                 int(max_chars) if max_chars else _MAX_TRANSCRIPT_CHARS)
    return (override_system or schema.system_prompt(lang, output_lang),
            schema.build_user_prompt(title, rows, lang))


def _trim(rows: Sequence[Dict[str, Any]], max_chars: int = _MAX_TRANSCRIPT_CHARS) -> list:
    """Bound the transcript length to the model's context budget."""
    out: list = []
    total = 0
    for r in rows:
        line = schema._line(r)
        if total + len(line) > max_chars and out:
            break
        out.append(r)
        total += len(line)
    return out


class AnalysisProcessor:
    """Runs the analysis pipeline for one meeting.

    Receiving the engine (rather than building it) keeps this class testable
    with a ``MockLLM`` and means no real LLM request is ever made in tests.
    """

    def __init__(self, config) -> None:
        self.config = config

    def process(
        self,
        meeting_id: str,
        engine: LLMEngine,
        kind: str = "summary",
        override_system: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        still_current: Optional[Callable[[], bool]] = None,
        output_lang: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 0) Size the transcript budget from the model's context window
        #    (best-effort detection; unknown window -> safe default).
        window = engine.get_context_window()
        max_chars = transcript_char_budget(self.config, window)

        # 1) Preconditions (do not flip status to failed for a bad precondition).
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None:
                raise KeyError(meeting_id)
            segs = s.scalars(
                select(TranscriptSegment).where(
                    TranscriptSegment.meeting_id == meeting_id
                ).order_by(TranscriptSegment.start_s)
            ).all()
            if not segs:
                raise LLMError(
                    "Kein Transkript vorhanden. Bitte das Meeting zuerst "
                    "transkribieren, bevor es analysiert wird."
                )
            rows = _trim(schema.to_seg_rows(
                [(seg.start_s, seg.end_s, seg.speaker_id, seg.text, seg.id)
                 for seg in segs]
            ), max_chars)
            title = meeting.title
            lang = meeting.lang
            # 2) Mark the resumable job + status (committed before the slow call).
            job = JobQueue.get_or_create(s, meeting_id, "analyze")
            JobQueue.mark_running(s, job)
            meeting.status = "analyzing"
            s.commit()

        def _cancelled() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return still_current is not None and not still_current()

        def _finish_cancelled() -> Dict[str, Any]:
            # A newer analysis for this meeting replaced this one: never touch
            # the job/status rows in that case.
            if still_current is not None and not still_current():
                return {"meeting_id": meeting_id, "status": "cancelled", "kind": kind}
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "analyze")
                if job.status in ("pending", "running"):
                    JobQueue.mark_cancelled(s, job)
                m = s.get(Meeting, meeting_id)
                if m is not None and m.status == "analyzing":
                    m.status = "ready"
                s.commit()
            log.info("analyze_cancelled meeting=%s kind=%s", meeting_id[:8], kind)
            return {"meeting_id": meeting_id, "status": "cancelled", "kind": kind}

        if _cancelled():
            return _finish_cancelled()

        system_prompt = override_system or schema.system_prompt(lang)
        user_prompt = schema.build_user_prompt(title, rows, lang)

        # 3) The (slow) LLM call happens outside any open DB session.
        t0 = time.monotonic()
        try:
            try:
                result = engine.complete(user_prompt, system=system_prompt,
                                         cancel_event=cancel_event)
            except LLMContextOverflowError as overflow:
                # The character budget is only an estimate of the token count;
                # if the server rejected the prompt anyway, shrink the
                # transcript by the server-reported ratio and retry once.
                rows, result = self._shrink_and_retry(
                    meeting_id, engine, title, rows, system_prompt, lang,
                    overflow, cancel_event)
            if _cancelled():
                return _finish_cancelled()
            raw = result.text
            model_used = result.model
            parsed = schema.extract_json(raw)
            normalised, errors = schema.validate_analysis(parsed, rows)
            # 3b) One automatic self-correction, then fail clearly (no invention).
            if errors:
                # Technical diagnosis only (answer length + reason) -- never the
                # full transcript or LLM answer, per the logging policy.
                log.warning(
                    "analyze_json_invalid meeting=%s raw_len=%d found_object=%s "
                    "errors=%d first=%s",
                    meeting_id[:8], len(raw or ""),
                    isinstance(parsed, dict), len(errors),
                    (errors[0] if errors else ""),
                )
                if _cancelled():
                    return _finish_cancelled()
                fix_prompt = schema.build_fix_prompt(errors, raw, rows)
                result2 = engine.complete(fix_prompt, system=schema.FIX_SYSTEM_PROMPT,
                                          cancel_event=cancel_event)
                if _cancelled():
                    return _finish_cancelled()
                raw = result2.text
                model_used = result2.model or model_used
                parsed = schema.extract_json(raw)
                normalised, errors = schema.validate_analysis(parsed, rows)
                if errors:
                    detail = "; ".join(errors[:8])
                    log.warning(
                        "analyze_json_still_invalid meeting=%s raw_len=%d "
                        "found_object=%s errors=%d",
                        meeting_id[:8], len(raw or ""),
                        isinstance(parsed, dict), len(errors),
                    )
                    raise LLMError(
                        "LLM lieferte ungültiges oder unvollständiges Analyse-JSON "
                        "(auch nach Korrekturversuch). Fehler: "
                        f"{detail}. Es wurde nichts gespeichert (keine Fakten erfunden)."
                    )
            content = json.dumps(normalised, ensure_ascii=False, indent=2)
            markdown = schema.render_markdown(normalised, lang=output_lang)
        except LLMCancelledError:
            return _finish_cancelled()
        except Exception as exc:  # noqa: BLE001 - normalise any failure to job/DB state
            if _cancelled():
                return _finish_cancelled()
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "analyze")
                JobQueue.mark_failed(s, job, str(exc))
                m = s.get(Meeting, meeting_id)
                if m is not None:
                    m.status = "failed"
                s.commit()
            log.error("analyze_failed meeting=%s kind=%s error=%s",
                      meeting_id[:8], kind, exc)
            raise

        if _cancelled():
            return _finish_cancelled()

        # 4) Persist the validated analysis (upsert per meeting+kind) and finalise.
        with session_scope() as s:
            job = JobQueue.get_or_create(s, meeting_id, "analyze")
            JobQueue.mark_done(s, job)
            analysis = s.scalar(
                select(Analysis).where(
                    Analysis.meeting_id == meeting_id, Analysis.kind == kind
                )
            )
            if analysis is None:
                analysis = Analysis(meeting_id=meeting_id, kind=kind)
                s.add(analysis)
                s.flush()
            analysis.model = model_used
            analysis.output_lang = output_lang
            analysis.content = content
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = "done"
            s.commit()
            if analysis.created_at:
                created = analysis.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created_at = created.astimezone(timezone.utc).isoformat()
            else:
                created_at = None

        log.info(
            "analyze_done meeting=%s kind=%s model=%s chars=%d window=%s "
            "transcript_chars=%d dur=%.2fs",
            meeting_id[:8], kind, model_used, len(content),
            window, len("\n".join(schema._line(r) for r in rows)),
            round(time.monotonic() - t0, 3),
        )
        return {
            "meeting_id": meeting_id,
            "status": "done",
            "kind": kind,
            "model": model_used,
            "output_lang": output_lang,
            "content": content,
            "markdown": markdown,
            "created_at": created_at,
        }

    def _shrink_and_retry(
        self,
        meeting_id: str,
        engine: LLMEngine,
        title: str,
        rows: Sequence[Dict[str, Any]],
        system_prompt: str,
        lang: Optional[str],
        overflow: LLMContextOverflowError,
        cancel_event: Optional[threading.Event],
    ) -> Tuple[list, LLMResult]:
        """Re-send the analysis with a shorter transcript after the server
        rejected the prompt as too large (``LLMContextOverflowError``).

        The transcript budget is a character-based estimate of the token
        count, which varies per tokenizer and per transcript content. The
        server reports how many tokens the prompt actually used and how many
        it offers; shrink the transcript by that ratio (times a safety
        margin), rebuild the prompt and retry exactly once. A second
        overflow, or a transcript that cannot be shrunk further, re-raises
        the original overflow error (which marks the job failed like any
        other ``LLMError``).
        """
        current = sum(len(schema._line(r)) for r in rows)
        if not overflow.prompt_tokens or not overflow.ctx_tokens or not rows:
            raise overflow
        factor = (overflow.ctx_tokens * _OVERFLOW_RETRY_MARGIN
                  / overflow.prompt_tokens)
        rows2 = _trim(rows, max(1, int(current * factor)))
        if not rows2 or len(rows2) >= len(rows):
            raise overflow
        log.warning(
            "analyze_context_overflow meeting=%s prompt_tokens=%d ctx_tokens=%d "
            "transcript_chars=%d->%d segments=%d->%d "
            "(retry with shrunk transcript)",
            meeting_id[:8], overflow.prompt_tokens, overflow.ctx_tokens,
            current, sum(len(schema._line(r)) for r in rows2),
            len(rows), len(rows2),
        )
        try:
            result = engine.complete(
                schema.build_user_prompt(title, rows2, lang),
                system=system_prompt, cancel_event=cancel_event)
        except LLMContextOverflowError:
            log.error(
                "analyze_context_overflow_again meeting=%s "
                "(shrunk transcript still too large; failing)", meeting_id[:8])
            raise overflow
        return list(rows2), result
