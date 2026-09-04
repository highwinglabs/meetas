"""Live (streaming) transcription (Phase 5).

A rolling-window pipeline that re-transcribes recent audio every few seconds and
emits finalized segments into the database while exposing the not-yet-settled
tail as a live "partial" for the UI. Optional local speaker diarization labels
the segments. Everything runs locally; the pipeline is opt-in via config.
"""
from core.live.engine import FasterWhisperLiveEngine, LiveASREngine
from core.live.pipeline import LiveTranscriptionPipeline

__all__ = [
    "LiveASREngine",
    "FasterWhisperLiveEngine",
    "LiveTranscriptionPipeline",
]
